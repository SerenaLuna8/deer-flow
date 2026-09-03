"""M10 T2 — base-level re-embedding of the current published content.

The rebuild path must stop re-parsing original files: it rebinds the
embedding model and queues ``reembed_document`` tasks that read the rows the
project actually has (manual edits, added segments, disabled segments) and
replace only vectors and generations. Identity, text, order, enabled state,
source positions, and hit counts survive; a never-published document is
skipped instead of silently re-ingested.

Everything runs against the installed Schema V1 snapshot in a disposable
PostgreSQL database; embeddings come from a deterministic fake client.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeError,
    KnowledgeRebuildResult,
    KnowledgeSegmentUpdate,
    KnowledgeSettings,
)
from actweave_knowledge.bases import KnowledgeBaseService
from actweave_knowledge.contracts import KNOWLEDGE_LEXICAL_VERSION
from actweave_knowledge.documents import KnowledgeDocumentService
from actweave_knowledge.extraction.contracts import ProcessingProfile
from actweave_knowledge.ingestion.reembed import KnowledgeReembedHandler
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentAttachmentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.persistence.tasks import (
    claim_next_task,
    settle_task_failure,
    settle_task_success,
)
from actweave_knowledge.retrieval import encode_lexical_token, lexical_index_input
from actweave_knowledge.segments import KnowledgeSegmentService
from actweave_knowledge.tasks import KnowledgeTaskClaim
from extraction_test_helpers import make_test_file_capability_provider, make_test_quota_port
from ingestion_test_helpers import ingestion_harness
from parsing_test_helpers import make_chunk_profile, make_parse_profile, write_docx_with_image
from registry_helpers import registry_model_port, seed_embedding_model, seed_provider
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge.composition import is_knowledge_project_active
from deerflow.persistence.bootstrap import _install_full_schema

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _FakeModelClient:
    """Deterministic embeddings whose values change with ``generation``."""

    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension
        self.generation = 1.0
        self.fail = False
        self.calls: list[list[str]] = []
        self.started = asyncio.Event()
        self.blocker: asyncio.Event | None = None

    async def embed(self, material, texts: list[str], *, kind="passage", batch_guard=None, on_batch_verified=None) -> list[list[float]]:  # noqa: ANN001
        # Batch hooks are exercised with the real client in
        # test_task_progress.py; this double stands for one successful batch.
        del batch_guard, on_batch_verified
        self.started.set()
        if self.blocker is not None:
            await self.blocker.wait()
        if self.fail:
            raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 调用失败")
        self.calls.append(list(texts))
        return [[self.generation * ((len(text) % 7) + 1)] * self.dimension for text in texts]


class _Harness:
    def __init__(self, engine, factory, client: _FakeModelClient) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.client = client
        self.handler = KnowledgeReembedHandler(
            session_factory=factory,
            model_client=client,  # type: ignore[arg-type]
            model_port=registry_model_port(),
        )

    def bases(self) -> KnowledgeBaseService:
        return KnowledgeBaseService(
            session_factory=self.factory,
            settings=KnowledgeSettings.model_validate({"enabled": False}),
            model_port=registry_model_port(),
        )

    def documents(self) -> KnowledgeDocumentService:
        return KnowledgeDocumentService(
            project_active_check=is_knowledge_project_active,
            quota=make_test_quota_port(self.factory),
            session_factory=self.factory,
            settings=KnowledgeSettings.model_validate({"enabled": False}),
            file_capabilities=make_test_file_capability_provider(),
            object_store=None,  # type: ignore[arg-type]
        )

    def segments(self) -> KnowledgeSegmentService:
        return KnowledgeSegmentService(
            session_factory=self.factory,
            settings=KnowledgeSettings.model_validate({"enabled": False}),
            client=self.client,  # type: ignore[arg-type]
            model_port=registry_model_port(),
        )


async def _harness(postgres_database_url: str) -> _Harness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    return _Harness(engine, factory, _FakeModelClient())


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
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"m10r_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"m10r-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _seed_base(
    harness: _Harness,
    *,
    dimension: int = 8,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Project + provider + embedding model + base; returns (project, base, model)."""

    provider_id = await seed_provider(harness.factory)
    embedding_model_id = await seed_embedding_model(harness.factory, provider_id, dimension=dimension)
    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = uuid.uuid4()
        session.add(
            KnowledgeBaseRow(
                id=base_id,
                project_id=project_id,
                name=f"base-{base_id.hex[:8]}",
                embedding_model_id=embedding_model_id,
                status="active",
            )
        )
    return project_id, base_id, embedding_model_id


async def _seed_document(
    harness: _Harness,
    project_id: uuid.UUID,
    base_id: uuid.UUID,
    *,
    status: str = "ready",
    version: int = 1,
    published_version: int | None = 1,
    chunking_mode: str = "general",
    parsing_profile: dict | None = None,
    segments: tuple[dict, ...] = (),
) -> uuid.UUID:
    """One document plus explicit segment rows on ``published_version``."""

    document_id = uuid.uuid4()
    async with harness.factory() as session, session.begin():
        session.add(
            KnowledgeDocumentRow(
                id=document_id,
                project_id=project_id,
                knowledge_base_id=base_id,
                name="note.md",
                original_name="note.md",
                storage_key=f"projects/{project_id}/knowledge/{base_id}/{document_id}.md",
                size_bytes=32,
                status=status,
                version=version,
                published_version=published_version,
                chunking_mode=chunking_mode,
                parsing_profile=parsing_profile,
                segment_count=len(segments),
                word_count=sum(len(seg["content"]) for seg in segments),
                error_message="失败原因" if status == "failed" else None,
            )
        )
        for position, seg in enumerate(segments, start=1):
            row = KnowledgeSegmentRow(
                id=seg.get("id", uuid.uuid4()),
                project_id=project_id,
                knowledge_base_id=base_id,
                knowledge_document_id=document_id,
                document_version=published_version if published_version is not None else version,
                position=position,
                content=seg["content"],
                index_text=seg.get("index_text", ""),
                token_count=seg.get("token_count", 0),
                source_spans=seg.get("source_spans", []),
                word_count=len(seg["content"]),
                enabled=seg.get("enabled", True),
                hit_count=seg.get("hit_count", 0),
                source_position=seg.get("source_position", {"page": position}),
                embedding=seg.get("embedding", [0.25] * 8 if chunking_mode == "general" else None),
            )
            session.add(row)
            await session.flush()
            for child_position, child_content in enumerate(seg.get("children", ()), start=1):
                session.add(
                    KnowledgeSegmentChildRow(
                        id=uuid.uuid4(),
                        project_id=project_id,
                        knowledge_base_id=base_id,
                        knowledge_document_id=document_id,
                        knowledge_segment_id=row.id,
                        document_version=published_version if published_version is not None else version,
                        position=child_position,
                        content=child_content,
                        word_count=len(child_content),
                        embedding=[0.5] * 8,
                    )
                )
    return document_id


async def _rebuild(
    harness: _Harness,
    project_id: uuid.UUID,
    base_id: uuid.UUID,
    *,
    dimension: int = 8,
) -> tuple[KnowledgeRebuildResult, uuid.UUID]:
    """Rebind to a fresh model of the same provider; returns (result, model_id)."""

    provider_id = await seed_provider(harness.factory)
    new_model_id = await seed_embedding_model(harness.factory, provider_id, dimension=dimension)
    result = await harness.bases().rebuild_knowledge_base(
        project_id,
        base_id,
        embedding_model_id=new_model_id,
    )
    return result, new_model_id


async def _claim(harness: _Harness) -> KnowledgeTaskClaim:
    async with harness.factory() as session, session.begin():
        row = await claim_next_task(session, lease_seconds=60)
        assert row is not None, "expected a claimable task"
        return KnowledgeTaskClaim(
            id=row.id,
            project_id=row.project_id,
            resource_id=row.resource_id,
            kind=row.kind,
            target_version=row.target_version,
            claim_token=row.claim_token,  # type: ignore[arg-type]
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            storage_key=row.storage_key,
        )


async def _segment_rows(harness: _Harness, document_id: uuid.UUID) -> list[KnowledgeSegmentRow]:
    async with harness.factory() as session:
        return list((await session.scalars(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document_id).order_by(KnowledgeSegmentRow.position))).all())


async def _child_rows(harness: _Harness, document_id: uuid.UUID) -> list[KnowledgeSegmentChildRow]:
    async with harness.factory() as session:
        return list((await session.scalars(select(KnowledgeSegmentChildRow).where(KnowledgeSegmentChildRow.knowledge_document_id == document_id).order_by(KnowledgeSegmentChildRow.position))).all())


async def _document_row(harness: _Harness, document_id: uuid.UUID) -> KnowledgeDocumentRow:
    async with harness.factory() as session:
        row = await session.get(KnowledgeDocumentRow, document_id)
        assert row is not None
        return row


async def _task_rows(harness: _Harness, document_id: uuid.UUID) -> list[KnowledgeTaskRow]:
    async with harness.factory() as session:
        return list((await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == document_id).order_by(KnowledgeTaskRow.created_at))).all())


# ---------------------------------------------------------------------------
# Rebuild admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_queues_reembed_and_reports_skipped_uninitialized(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        ready_id = await _seed_document(harness, project_id, base_id, segments=({"content": "已发布内容"},))
        failed_published_id = await _seed_document(
            harness,
            project_id,
            base_id,
            status="failed",
            version=2,
            published_version=1,
            segments=({"content": "上代内容仍在"},),
        )
        never_published_id = await _seed_document(
            harness,
            project_id,
            base_id,
            status="failed",
            version=1,
            published_version=None,
        )

        result, new_model_id = await _rebuild(harness, project_id, base_id)

        assert type(result) is KnowledgeRebuildResult
        assert result.base.embedding_model_id == new_model_id
        assert result.accepted_document_count == 2
        assert result.skipped_document_ids == (never_published_id,)

        ready_row = await _document_row(harness, ready_id)
        assert (ready_row.status, ready_row.version) == ("queued", 2)
        # Content survives admission: counters keep describing the rows.
        assert ready_row.segment_count == 1
        assert ready_row.word_count == len("已发布内容")
        assert ready_row.published_version == 1

        failed_row = await _document_row(harness, failed_published_id)
        assert (failed_row.status, failed_row.version) == ("queued", 3)
        assert failed_row.error_message is None

        skipped_row = await _document_row(harness, never_published_id)
        assert (skipped_row.status, skipped_row.version) == ("failed", 1)

        ready_tasks = await _task_rows(harness, ready_id)
        assert [(task.kind, task.target_version, task.status) for task in ready_tasks] == [("reembed_document", 2, "queued")]
        assert await _task_rows(harness, never_published_id) == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_rebuild_rejects_in_flight_documents_and_open_indexing_tasks(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        for blocking_status in ("uploading", "queued", "processing", "deleting"):
            project_id, base_id, _ = await _seed_base(harness)
            await _seed_document(harness, project_id, base_id, segments=({"content": "内容"},))
            await _seed_document(
                harness,
                project_id,
                base_id,
                status=blocking_status,
                published_version=None,
            )
            with pytest.raises(KnowledgeError) as error:
                await _rebuild(harness, project_id, base_id)
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST, blocking_status

        # A failed document whose ingest is waiting for its automatic retry
        # still owns the open-indexing slot; rebuilding now would race it.
        project_id, base_id, _ = await _seed_base(harness)
        failed_id = await _seed_document(
            harness,
            project_id,
            base_id,
            status="failed",
            published_version=1,
            segments=({"content": "旧内容"},),
        )
        async with harness.factory() as session, session.begin():
            session.add(
                KnowledgeTaskRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    resource_id=failed_id,
                    kind="ingest_document",
                    target_version=1,
                    status="retry_wait",
                    attempt_count=1,
                )
            )
        with pytest.raises(KnowledgeError) as error:
            await _rebuild(harness, project_id, base_id)
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Re-embed handler: identity preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reembed_preserves_manual_content_and_identity_general(postgres_database_url: str) -> None:
    """The T2 headline: manual edits survive a model rebind.

    Under the pre-M10 implementation the rebuild re-parsed the original file,
    so the manually edited segment text below would be lost. Re-embedding must
    keep every row (UUID, text, order, enabled, source_position, hit_count)
    and change only vectors and generations.
    """

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        edited_id = uuid.uuid4()
        manual_id = uuid.uuid4()
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            segments=(
                {"id": edited_id, "content": "人工编辑过的分段", "hit_count": 7, "embedding": [0.1] * 8},
                {"id": manual_id, "content": "手工新增的分段", "source_position": {"manual": True}, "enabled": False, "embedding": [0.2] * 8},
            ),
        )

        harness.client.generation = 9.0
        result, new_model_id = await _rebuild(harness, project_id, base_id)
        assert result.accepted_document_count == 1

        claim = await _claim(harness)
        assert claim.kind == "reembed_document"
        await harness.handler(claim)

        document = await _document_row(harness, document_id)
        assert document.status == "ready"
        assert document.version == 2
        assert document.published_version == 2
        assert document.segment_count == 2
        assert document.word_count == len("人工编辑过的分段") + len("手工新增的分段")

        rows = await _segment_rows(harness, document_id)
        assert [row.id for row in rows] == [edited_id, manual_id]
        assert [row.content for row in rows] == ["人工编辑过的分段", "手工新增的分段"]
        assert [row.enabled for row in rows] == [True, False]
        assert [row.hit_count for row in rows] == [7, 0]
        assert rows[1].source_position == {"manual": True}
        assert all(row.document_version == 2 for row in rows)
        # Vectors moved to the new model's space (generation 9 fake values).
        assert all(list(row.embedding)[0] >= 9.0 for row in rows)

        # The disabled segment was embedded too: re-enabling must not find a
        # missing or stale vector.
        assert sorted(text for call in harness.client.calls for text in call) == sorted(["人工编辑过的分段", "手工新增的分段"])

        tasks = await _task_rows(harness, document_id)
        assert [task.status for task in tasks] == ["succeeded"]

        # No original-file machinery was touched: the handler has no object
        # store at all, so passing this test proves no download happened.
        base = await _base_row(harness, base_id)
        assert base.embedding_model_id == new_model_id
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reembed_uses_saved_index_text_and_preserves_display_derivations(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        segment_id = uuid.uuid4()
        content = "# 手册\n\n运行 `actweave up`。"
        index_text = "手册\n运行 actweave up。"
        source_spans = [
            {
                "block_id": "paragraph:1",
                "start": 0,
                "end": len(content),
                "location": {"paragraph": 1},
                "role": "source",
            }
        ]
        profile = {
            "parse": make_parse_profile(".md").model_dump(mode="json"),
            "chunk": make_chunk_profile().model_dump(mode="json"),
        }
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            parsing_profile=profile,
            segments=(
                {
                    "id": segment_id,
                    "content": content,
                    "index_text": index_text,
                    "token_count": 8,
                    "source_spans": source_spans,
                    "enabled": False,
                },
            ),
        )

        await _rebuild(harness, project_id, base_id)
        await harness.handler(await _claim(harness))

        assert harness.client.calls == [[index_text]]
        [row] = await _segment_rows(harness, document_id)
        assert row.id == segment_id
        assert row.content == content
        assert row.index_text == index_text
        assert row.token_count == 8
        assert row.source_spans == source_spans
        assert row.enabled is False
        assert (await _document_row(harness, document_id)).parsing_profile == profile
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reembed_token_profile_refuses_missing_index_text_before_provider(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        profile = {
            "parse": make_parse_profile(".md").model_dump(mode="json"),
            "chunk": make_chunk_profile().model_dump(mode="json"),
        }
        await _seed_document(
            harness,
            project_id,
            base_id,
            parsing_profile=profile,
            segments=({"content": "非空正文但索引列缺失", "index_text": ""},),
        )
        await _rebuild(harness, project_id, base_id)

        with pytest.raises(KnowledgeError) as error:
            await harness.handler(await _claim(harness))
        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
        assert harness.client.calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reembed_reads_no_original_and_preserves_bindings_and_profile(
    postgres_database_url: str,
    tmp_path,
) -> None:
    source = tmp_path / "guide.docx"
    write_docx_with_image(source)
    profile = ProcessingProfile(
        parse=make_parse_profile(".docx"),
        chunk=make_chunk_profile(),
    )
    async with ingestion_harness(postgres_database_url) as harness:
        document = await harness.upload(source, profile)
        await harness.run_next_task()
        before = await harness.segments(document.id)
        await harness.module.update_segment(
            harness.resources.project_id,
            before[0].id,
            KnowledgeSegmentUpdate(enabled=False),
            authority=harness.authority,
        )
        before = await harness.segments(document.id)
        async with harness.resources.session_factory() as session:
            document_row = await session.get(KnowledgeDocumentRow, document.id)
            assert document_row is not None
            before_profile = document_row.parsing_profile
            before_bindings = list(
                (
                    await session.execute(
                        select(
                            KnowledgeSegmentAttachmentRow.segment_id,
                            KnowledgeSegmentAttachmentRow.position,
                            KnowledgeSegmentAttachmentRow.attachment_id,
                            KnowledgeSegmentAttachmentRow.extraction_id,
                            KnowledgeSegmentAttachmentRow.alt_text,
                        )
                        .where(KnowledgeSegmentAttachmentRow.knowledge_document_id == document.id)
                        .order_by(
                            KnowledgeSegmentAttachmentRow.segment_id,
                            KnowledgeSegmentAttachmentRow.position,
                        )
                    )
                ).all()
            )
        before_gets = len([call for call in harness.resources.object_store.calls if call[0] == "get"])
        # The shared Extraction harness owns an unrelated already-claimed
        # bootstrap document. Terminalize that fixture so this base-level
        # rebuild is scoped to the uploaded publication under test.
        async with harness.resources.session_factory() as session, session.begin():
            bootstrap = await session.get(
                KnowledgeDocumentRow,
                harness.resources.document_id,
                with_for_update=True,
            )
            assert bootstrap is not None and bootstrap.published_version is None
            bootstrap.status = "failed"
            bootstrap.error_message = "test fixture is not published"
            assert await settle_task_success(
                session,
                harness.resources.claim.id,
                harness.resources.claim.claim_token,
            )
        harness.resources.object_store.fail_next("get")
        harness.fake_model.calls.clear()

        await harness.reembed(harness.resources.base_id)
        await harness.run_next_task()

        after = await harness.segments(document.id)
        assert [row.id for row in after] == [row.id for row in before]
        assert [row.content for row in after] == [row.content for row in before]
        assert [row.index_text for row in after] == [row.index_text for row in before]
        assert [row.source_spans for row in after] == [row.source_spans for row in before]
        assert [row.enabled for row in after] == [row.enabled for row in before]
        assert len([call for call in harness.resources.object_store.calls if call[0] == "get"]) == before_gets
        async with harness.resources.session_factory() as session:
            document_row = await session.get(KnowledgeDocumentRow, document.id)
            assert document_row is not None
            assert document_row.parsing_profile == before_profile
            after_bindings = list(
                (
                    await session.execute(
                        select(
                            KnowledgeSegmentAttachmentRow.segment_id,
                            KnowledgeSegmentAttachmentRow.position,
                            KnowledgeSegmentAttachmentRow.attachment_id,
                            KnowledgeSegmentAttachmentRow.extraction_id,
                            KnowledgeSegmentAttachmentRow.alt_text,
                        )
                        .where(KnowledgeSegmentAttachmentRow.knowledge_document_id == document.id)
                        .order_by(
                            KnowledgeSegmentAttachmentRow.segment_id,
                            KnowledgeSegmentAttachmentRow.position,
                        )
                    )
                ).all()
            )
        assert after_bindings == before_bindings


async def _base_row(harness: _Harness, base_id: uuid.UUID) -> KnowledgeBaseRow:
    async with harness.factory() as session:
        row = await session.get(KnowledgeBaseRow, base_id)
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_reembed_never_touches_lexical_fields(postgres_database_url: str) -> None:
    """T8: content is unchanged by a re-embed, so its lexical derivation is too."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        document_id = await _seed_document(harness, project_id, base_id, segments=({"content": "网络手册"},))
        # Stand in for what the publish transaction would have derived.
        async with harness.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE knowledge_segments
                       SET lexical_tsv = to_tsvector('simple', :input), lexical_version = :version
                       WHERE knowledge_document_id = :document_id"""
                ),
                {
                    "input": lexical_index_input("网络手册"),
                    "version": KNOWLEDGE_LEXICAL_VERSION,
                    "document_id": document_id,
                },
            )

        await _rebuild(harness, project_id, base_id)
        claim = await _claim(harness)
        assert claim.kind == "reembed_document"
        await harness.handler(claim)

        [row] = await _segment_rows(harness, document_id)
        assert row.document_version == 2  # the re-embed did publish
        async with harness.factory() as session:
            lexical_version, still_matches = (
                await session.execute(
                    text(
                        """SELECT lexical_version, lexical_tsv @@ to_tsquery('simple', :token)
                           FROM knowledge_segments WHERE id = :id"""
                    ),
                    {"token": encode_lexical_token("网络"), "id": row.id},
                )
            ).one()
        assert lexical_version == KNOWLEDGE_LEXICAL_VERSION
        assert still_matches is True
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reembed_parent_child_embeds_children_only(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        parent_id = uuid.uuid4()
        legacy_profile = {
            "parse": make_parse_profile(".md").model_dump(mode="json"),
            "chunk": make_chunk_profile(
                unit="character",
                mode="parent_child",
            ).model_dump(mode="json"),
        }
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            chunking_mode="parent_child",
            parsing_profile=legacy_profile,
            segments=(
                {
                    "id": parent_id,
                    "content": "父段完整内容",
                    "embedding": None,
                    "children": ("子块一", "子块二"),
                },
            ),
        )

        harness.client.generation = 5.0
        await _rebuild(harness, project_id, base_id)
        await harness.handler(await _claim(harness))

        document = await _document_row(harness, document_id)
        assert (document.status, document.version, document.published_version) == ("ready", 2, 2)

        [parent] = await _segment_rows(harness, document_id)
        assert parent.id == parent_id
        assert parent.embedding is None
        assert parent.document_version == 2

        children = await _child_rows(harness, document_id)
        assert [child.content for child in children] == ["子块一", "子块二"]
        assert all(child.document_version == 2 for child in children)
        assert all(list(child.embedding)[0] >= 5.0 for child in children)

        # Only children were embedded; the parent text never went to the model.
        assert sorted(text for call in harness.client.calls for text in call) == ["子块一", "子块二"]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reembed_empty_initialized_document_publishes_with_zero_rows(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        document_id = await _seed_document(harness, project_id, base_id, segments=())

        await _rebuild(harness, project_id, base_id)
        await harness.handler(await _claim(harness))

        document = await _document_row(harness, document_id)
        assert (document.status, document.version, document.published_version) == ("ready", 2, 2)
        assert document.segment_count == 0
        assert harness.client.calls == []
        tasks = await _task_rows(harness, document_id)
        assert [task.status for task in tasks] == ["succeeded"]
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Failure, late results, retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reembed_failure_keeps_rows_and_published_version(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        segment_id = uuid.uuid4()
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            segments=({"id": segment_id, "content": "重要的人工内容", "embedding": [0.3] * 8},),
        )

        await _rebuild(harness, project_id, base_id)
        claim = await _claim(harness)
        harness.client.fail = True
        with pytest.raises(KnowledgeError) as error:
            await harness.handler(claim)
        assert error.value.code == KNOWLEDGE_EMBEDDING_FAILED
        async with harness.factory() as session, session.begin():
            outcome = await settle_task_failure(
                session,
                claim.id,
                claim.claim_token,
                error_message=error.value.message,
                retry_delay_seconds=0,
            )
        assert outcome == "retry_wait"

        # Attempts remain: the document stays queued for the automatic retry.
        document = await _document_row(harness, document_id)
        assert document.published_version == 1
        [row] = await _segment_rows(harness, document_id)
        assert row.id == segment_id
        assert row.content == "重要的人工内容"
        assert row.document_version == 1
        assert list(row.embedding) == [0.3] * 8

        # Exhaust the remaining attempts: the document must derive ``failed``
        # while rows and published_version stay put.
        harness.client.fail = True
        while True:
            claim = await _claim(harness)
            with pytest.raises(KnowledgeError):
                await harness.handler(claim)
            async with harness.factory() as session, session.begin():
                outcome = await settle_task_failure(
                    session,
                    claim.id,
                    claim.claim_token,
                    error_message="Embedding 调用失败",
                    retry_delay_seconds=0,
                )
            if outcome == "failed":
                break

        document = await _document_row(harness, document_id)
        assert document.status == "failed"
        assert document.error_message == "Embedding 调用失败"
        assert document.published_version == 1
        [row] = await _segment_rows(harness, document_id)
        assert row.document_version == 1
        assert list(row.embedding) == [0.3] * 8
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reembed_late_result_never_publishes(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            segments=({"content": "内容", "embedding": [0.4] * 8},),
        )

        await _rebuild(harness, project_id, base_id)
        claim = await _claim(harness)
        # Another admission (retry, second rebuild) bumps the version while
        # this claim is in flight: the publish must become a no-op.
        async with harness.factory() as session, session.begin():
            row = await session.get(KnowledgeDocumentRow, document_id)
            assert row is not None
            row.version = row.version + 1

        await harness.handler(claim)

        document = await _document_row(harness, document_id)
        assert document.status == "queued"
        assert document.published_version == 1
        [segment] = await _segment_rows(harness, document_id)
        assert segment.document_version == 1
        assert list(segment.embedding) == [0.4] * 8
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reembed_skips_document_that_started_deleting(postgres_database_url: str) -> None:
    """Deletion admitted after the rebuild wins: the claim becomes a no-op.

    Rebuild admission rejects deleting documents, but a deletion accepted
    while the re-embed task waits in the queue flips the status before the
    claim starts. The handler must leave the document (and its rows) to the
    deletion flow instead of processing or publishing anything.
    """

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            segments=({"content": "内容", "embedding": [0.5] * 8},),
        )

        await _rebuild(harness, project_id, base_id)
        async with harness.factory() as session, session.begin():
            row = await session.get(KnowledgeDocumentRow, document_id)
            assert row is not None
            row.status = "deleting"

        await harness.handler(await _claim(harness))

        assert harness.client.calls == []
        document = await _document_row(harness, document_id)
        assert (document.status, document.published_version) == ("deleting", 1)
        [segment] = await _segment_rows(harness, document_id)
        assert segment.document_version == 1
        assert list(segment.embedding) == [0.5] * 8
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_manual_retry_of_failed_reembed_keeps_the_reembed_kind(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        segment_id = uuid.uuid4()
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            segments=({"id": segment_id, "content": "保留的内容", "embedding": [0.6] * 8},),
        )

        await _rebuild(harness, project_id, base_id)
        harness.client.fail = True
        while True:
            claim = await _claim(harness)
            with pytest.raises(KnowledgeError):
                await harness.handler(claim)
            async with harness.factory() as session, session.begin():
                outcome = await settle_task_failure(
                    session,
                    claim.id,
                    claim.claim_token,
                    error_message="Embedding 调用失败",
                    retry_delay_seconds=0,
                )
            if outcome == "failed":
                break

        view = await harness.documents().retry_document(project_id, document_id)
        assert view.status == "queued"
        # Retry inherits the re-embed semantics: content rows must survive,
        # so the counters keep describing them instead of resetting to zero.
        assert view.segment_count == 1

        tasks = await _task_rows(harness, document_id)
        assert [task.kind for task in tasks] == ["reembed_document", "reembed_document"]
        latest = tasks[-1]
        assert (latest.status, latest.target_version) == ("queued", 3)

        harness.client.fail = False
        harness.client.generation = 4.0
        await harness.handler(await _claim(harness))

        document = await _document_row(harness, document_id)
        assert (document.status, document.version, document.published_version) == ("ready", 3, 3)
        [row] = await _segment_rows(harness, document_id)
        assert row.id == segment_id
        assert row.content == "保留的内容"
        assert row.document_version == 3
        assert list(row.embedding)[0] >= 4.0
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Late segment edits after identity-preserving re-embeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_segment_edit_conflicts_instead_of_writing_stale_vectors(postgres_database_url: str) -> None:
    """A segment edit that embedded before a re-embed must not publish after
    it — even though the row still exists under the same UUID, the document
    is ready again, and (same-model re-embed) the binding never changed.
    Only the frozen pre-provider-call version comparison catches this."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, embedding_model_id = await _seed_base(harness)
        segment_id = uuid.uuid4()
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            segments=({"id": segment_id, "content": "原内容", "embedding": [0.7] * 8},),
        )

        blocker = asyncio.Event()
        harness.client.blocker = blocker
        edit = asyncio.create_task(
            harness.segments().update_segment(
                project_id,
                segment_id,
                KnowledgeSegmentUpdate(content="迟到的编辑"),
            )
        )
        await harness.client.started.wait()

        # While the edit's embedding call is in flight, a plain re-embed on
        # the *same* model republishes the document: binding unchanged,
        # version bumped.
        harness.client.blocker = None
        await harness.bases().rebuild_knowledge_base(
            project_id,
            base_id,
            embedding_model_id=embedding_model_id,
        )
        await harness.handler(await _claim(harness))
        document = await _document_row(harness, document_id)
        assert document.status == "ready"

        blocker.set()
        with pytest.raises(KnowledgeError) as error:
            await edit
        assert error.value.code == KNOWLEDGE_CONFLICT

        [row] = await _segment_rows(harness, document_id)
        assert row.content == "原内容"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reembed_after_failed_mode_switching_reparse_uses_published_mode(postgres_database_url: str) -> None:
    """A failed general→parent_child re-parse leaves the published rows in
    general mode, so a subsequent re-embed must embed the parent segments —
    the stored (still general) mode — not the never-published new mode."""

    from actweave_knowledge import KnowledgeReparseRequest

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, _ = await _seed_base(harness)
        document_id = await _seed_document(
            harness,
            project_id,
            base_id,
            segments=({"content": "已发布的父段内容", "embedding": [0.2] * 8},),
        )

        # Queue an explicit re-parse that would switch to parent_child, then
        # exhaust its attempts without any handler ever publishing.
        await harness.documents().reparse_document(
            project_id,
            document_id,
            KnowledgeReparseRequest(
                expected_version=1,
                chunking_mode="parent_child",
                child_chunk_size=200,
            ),
        )
        while True:
            async with harness.factory() as session, session.begin():
                row = await claim_next_task(session, lease_seconds=60)
                assert row is not None
                outcome = await settle_task_failure(
                    session,
                    row.id,
                    row.claim_token,  # type: ignore[arg-type]
                    error_message="解析失败",
                    retry_delay_seconds=0,
                )
            if outcome == "failed":
                break

        failed = await _document_row(harness, document_id)
        assert failed.status == "failed"
        assert failed.chunking_mode == "general"
        assert failed.published_version == 1

        result, _ = await _rebuild(harness, project_id, base_id)
        assert result.accepted_document_count == 1
        await harness.handler(await _claim(harness))

        document = await _document_row(harness, document_id)
        assert document.status == "ready"
        assert document.chunking_mode == "general"
        # The re-embed read the published parent rows, general mode.
        assert harness.client.calls == [["已发布的父段内容"]]
        [segment] = await _segment_rows(harness, document_id)
        assert segment.embedding is not None
        assert await _child_rows(harness, document_id) == []
    finally:
        await harness.engine.dispose()
