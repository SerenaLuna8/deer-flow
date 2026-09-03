"""``relex_document``: rebuild lexical derivations from stored ``index_text``.

Bumping ``KNOWLEDGE_LEXICAL_VERSION`` used to leave every hybrid base failing
with ``KNOWLEDGE_CONFLICT`` until each document was re-parsed (sandbox
extraction plus a full re-embed). The lexical columns are pure derivations of
the persisted model text, so a base-level ``relex`` admits one cheap Worker
task per stale published document and the handler rewrites ``lexical_tsv`` /
``lexical_version`` in place — no parser, object store, or Provider access,
documents stay ``ready`` throughout, and content that changes underneath a
claim is never overwritten with stale tokens.
"""

from __future__ import annotations

import uuid

import pytest
from actweave_knowledge import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_LEXICAL_VERSION,
    KNOWLEDGE_NOT_FOUND,
    KnowledgeBaseCreate,
    KnowledgeError,
)
from actweave_knowledge.ingestion.relex import KnowledgeRelexHandler
from actweave_knowledge.persistence.models import (
    KnowledgeDocumentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.persistence.tasks import claim_next_task
from actweave_knowledge.retrieval import lexical_index_input
from actweave_knowledge.tasks import KnowledgeTaskClaim
from registry_helpers import seed_registry_models
from sqlalchemy import select, text
from test_bases import _harness, _seed_document, _seed_project


async def _seed_base(harness, project_id: uuid.UUID, *, retrieval_mode: str = "hybrid"):  # noqa: ANN001
    embedding_model_id, _ = await seed_registry_models(harness.factory)
    created = await harness.service.create_knowledge_base(
        project_id,
        KnowledgeBaseCreate(name=f"词法库-{uuid.uuid4().hex[:6]}", embedding_model_id=embedding_model_id, retrieval_mode=retrieval_mode),  # type: ignore[arg-type]
    )
    return created


async def _seed_rows(
    session,  # noqa: ANN001
    document: KnowledgeDocumentRow,
    *,
    contents: list[str],
    lexical_version: int,
    children: dict[int, list[str]] | None = None,
) -> list[KnowledgeSegmentRow]:
    session.add(document)
    await session.flush()
    rows: list[KnowledgeSegmentRow] = []
    for position, content in enumerate(contents, start=1):
        segment = KnowledgeSegmentRow(
            id=uuid.uuid4(),
            project_id=document.project_id,
            knowledge_base_id=document.knowledge_base_id,
            knowledge_document_id=document.id,
            document_version=document.version,
            position=position,
            content=content,
            index_text=content,
            word_count=len(content),
            embedding=None if children else [1.0, 0.0, 0.0],
            lexical_version=lexical_version,
        )
        session.add(segment)
        await session.flush()
        for child_position, child in enumerate((children or {}).get(position, ()), start=1):
            session.add(
                KnowledgeSegmentChildRow(
                    id=uuid.uuid4(),
                    project_id=document.project_id,
                    knowledge_base_id=document.knowledge_base_id,
                    knowledge_document_id=document.id,
                    knowledge_segment_id=segment.id,
                    document_version=document.version,
                    position=child_position,
                    content=child,
                    index_text=child,
                    word_count=len(child),
                    embedding=[1.0, 0.0, 0.0],
                    lexical_version=lexical_version,
                )
            )
        rows.append(segment)
    return rows


async def _claim(harness, *, project_id: uuid.UUID) -> KnowledgeTaskClaim:  # noqa: ANN001
    async with harness.factory() as session, session.begin():
        row = await claim_next_task(session, lease_seconds=60)
        assert row is not None and row.project_id == project_id
        return KnowledgeTaskClaim(
            id=row.id,
            project_id=row.project_id,
            resource_id=row.resource_id,
            kind=row.kind,
            target_version=row.target_version,
            claim_token=row.claim_token,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
        )


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relex_admits_one_task_per_stale_published_document(postgres_database_url: str) -> None:
    """Stale ready documents each get a ``relex_document`` task on their
    current version and stay ``ready``; current rows and never-published
    documents are reported, not queued; an open indexing slot skips the doc."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base = await _seed_base(harness, project_id)
        async with harness.factory() as session, session.begin():
            stale = _seed_document(project_id, base.id, name="stale", status="ready", version=2, published_version=2)
            await _seed_rows(session, stale, contents=["错误码e404排查手册"], lexical_version=KNOWLEDGE_LEXICAL_VERSION - 1)
            current = _seed_document(project_id, base.id, name="current", status="ready")
            await _seed_rows(session, current, contents=["普通安装说明"], lexical_version=KNOWLEDGE_LEXICAL_VERSION)
            never = _seed_document(project_id, base.id, name="never", status="failed", published_version=None)
            session.add(never)
            busy = _seed_document(project_id, base.id, name="busy", status="ready")
            await _seed_rows(session, busy, contents=["忙碌文档"], lexical_version=0)
            await session.flush()
            session.add(KnowledgeTaskRow(id=uuid.uuid4(), project_id=project_id, resource_id=busy.id, kind="summarize_document", target_version=1))

        result = await harness.service.relex_knowledge_base(project_id, base.id)

        assert result.accepted_document_count == 1
        assert result.up_to_date_document_count == 1
        assert set(result.skipped_document_ids) == {never.id, busy.id}
        async with harness.factory() as session:
            tasks = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.kind == "relex_document", KnowledgeTaskRow.project_id == project_id))).all()
            documents = {row.name: row for row in (await session.scalars(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == base.id))).all()}
        assert [(task.resource_id, task.target_version, task.status) for task in tasks] == [(stale.id, 2, "queued")]
        # Re-deriving never takes a document out of ready or bumps its version.
        assert documents["stale"].status == "ready"
        assert documents["stale"].version == 2
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_relex_rejects_missing_or_deleting_bases(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base = await _seed_base(harness, project_id)
        with pytest.raises(KnowledgeError) as missing:
            await harness.service.relex_knowledge_base(project_id, uuid.uuid4())
        assert missing.value.code == KNOWLEDGE_NOT_FOUND
        with pytest.raises(KnowledgeError) as outsider:
            await harness.service.relex_knowledge_base(uuid.uuid4(), base.id)
        assert outsider.value.code == KNOWLEDGE_NOT_FOUND
        async with harness.factory() as session, session.begin():
            await session.execute(text("UPDATE knowledge_bases SET status = 'deleting' WHERE id = :id"), {"id": base.id})
        with pytest.raises(KnowledgeError) as deleting:
            await harness.service.relex_knowledge_base(project_id, base.id)
        assert deleting.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relex_handler_rewrites_segment_and_child_derivations_in_place(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base = await _seed_base(harness, project_id)
        async with harness.factory() as session, session.begin():
            document = _seed_document(project_id, base.id, name="doc", status="ready", version=3, published_version=3)
            document.chunking_mode = "parent_child"
            [parent] = await _seed_rows(
                session,
                document,
                contents=["错误码e404排查。常规处理流程。"],
                lexical_version=0,
                children={1: ["e404报错处理", "常规处理流程"]},
            )
            # A row from an older generation must be left alone.
            old = KnowledgeSegmentRow(
                id=uuid.uuid4(),
                project_id=project_id,
                knowledge_base_id=base.id,
                knowledge_document_id=document.id,
                document_version=2,
                position=1,
                content="旧版本内容",
                index_text="旧版本内容",
                embedding=[1.0, 0.0, 0.0],
                lexical_version=0,
            )
            session.add(old)
        assert await harness.service.relex_knowledge_base(project_id, base.id) is not None
        claim = await _claim(harness, project_id=project_id)

        await KnowledgeRelexHandler(session_factory=harness.factory, project_active_check=None)(claim)

        async with harness.factory() as session:
            segments = (await session.execute(text("SELECT id, document_version, lexical_version, lexical_tsv::text FROM knowledge_segments WHERE knowledge_document_id = :doc"), {"doc": document.id})).all()
            children = (await session.execute(text("SELECT content, lexical_version, lexical_tsv::text FROM knowledge_segment_children WHERE knowledge_document_id = :doc ORDER BY position"), {"doc": document.id})).all()
            expected_parent = (await session.execute(text("SELECT to_tsvector('simple', :input)::text"), {"input": lexical_index_input("错误码e404排查。常规处理流程。")})).scalar_one()
            task = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.id == claim.id))).one()
            row = await session.get(KnowledgeDocumentRow, document.id)
        by_version = {version: (lexical_version, tsv) for _, version, lexical_version, tsv in segments}
        assert by_version[3] == (KNOWLEDGE_LEXICAL_VERSION, expected_parent)
        assert by_version[2][0] == 0  # the old generation keeps its placeholder
        assert [version for _, version, _ in children] == [KNOWLEDGE_LEXICAL_VERSION, KNOWLEDGE_LEXICAL_VERSION]
        assert all(tsv != "" for _, _, tsv in children)
        assert (task.status, task.stage, task.completed_units, task.total_units) == ("succeeded", "done", 3, 3)
        assert row is not None and row.status == "ready" and row.version == 3
        del parent
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_relex_handler_settles_without_writes_when_the_document_moved_on(postgres_database_url: str) -> None:
    """A newer generation (retry/reparse) after admission makes the claim a
    no-op: nothing is rewritten and the task settles successfully."""

    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base = await _seed_base(harness, project_id)
        async with harness.factory() as session, session.begin():
            document = _seed_document(project_id, base.id, name="doc", status="ready")
            await _seed_rows(session, document, contents=["错误码e404排查手册"], lexical_version=0)
        await harness.service.relex_knowledge_base(project_id, base.id)
        claim = await _claim(harness, project_id=project_id)
        async with harness.factory() as session, session.begin():
            await session.execute(text("UPDATE knowledge_documents SET version = 2, status = 'queued' WHERE id = :id"), {"id": document.id})

        await KnowledgeRelexHandler(session_factory=harness.factory, project_active_check=None)(claim)

        async with harness.factory() as session:
            versions = (await session.scalars(text("SELECT lexical_version FROM knowledge_segments WHERE knowledge_document_id = :doc"), {"doc": document.id})).all()
            task = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.id == claim.id))).one()
        assert versions == [0]
        assert task.status == "succeeded"
    finally:
        await harness.engine.dispose()
