"""Critical ingest/reparse lifecycle and retained character-profile regressions.

Pipeline tests use real PostgreSQL with fake object storage and model transport;
dedicated extractor tests own the individual file-format matrix.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from pathlib import Path

import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_PARSE_FAILED,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeChunkPreviewRequest,
    KnowledgeError,
    KnowledgeReparseRequest,
    KnowledgeSettings,
)
from actweave_knowledge.contracts import KNOWLEDGE_LEXICAL_VERSION
from actweave_knowledge.documents import KnowledgeDocumentService
from actweave_knowledge.extraction.contracts import ProcessingProfile
from actweave_knowledge.ingestion import (
    ExtractedBlock,
    KnowledgeIngestionHandler,
    clean_text,
    preview_document_chunks,
    split_blocks,
)
from actweave_knowledge.ingestion import pipeline as pipeline_module
from actweave_knowledge.ingestion.splitter import (
    split_child_chunks,
)
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.persistence.tasks import claim_next_task
from actweave_knowledge.retrieval import encode_lexical_token, lexical_v1_tokens
from actweave_knowledge.tasks import KnowledgeTaskClaim, KnowledgeTaskWorker
from extraction_test_helpers import (
    ExtractionObjectStore,
    make_test_file_capability_provider,
    make_test_quota_port,
)
from ingestion_test_helpers import FakeModelClient as _FakeModelClient
from parsing_test_helpers import make_chunk_profile, make_parse_profile
from registry_helpers import registry_model_port, seed_embedding_model, seed_provider
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge.composition import is_knowledge_project_active
from deerflow.persistence.model_registry import ModelProviderModelRow

# ---------------------------------------------------------------------------
# End-to-end preview and retained character-profile behavior
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docx_table_preview_keeps_fault_code_and_procedure_in_one_segment(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "E42"
    table.cell(0, 1).text = "重启网关服务"
    path = tmp_path / "procedures.docx"
    document.save(str(path))

    preview = await _preview_chunks(_preview_request(path), KnowledgeSettings.model_validate({"enabled": False}))

    assert preview.total == 1
    assert preview.chunks[0].content == "列1：E42\n列2：重启网关服务"


def test_extract_rejects_zip_bomb_before_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import zipfile

    from actweave_knowledge.ingestion import extractor

    monkeypatch.setattr(extractor, "_ZIP_BYTES_FLOOR", 1024)
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\0" * 1025)
    with pytest.raises(KnowledgeError) as error:
        extractor.extract_blocks(path, ".docx", max_total_chars=1)
    assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED


# ---------------------------------------------------------------------------
# Cleaner and splitter
# ---------------------------------------------------------------------------


def test_split_carries_whole_trailing_pieces_as_overlap() -> None:
    # Six 40-char sentences split on ". " (42-char pieces with the suffix).
    # Four pieces fill a 200-char chunk; overlap 60 retains exactly one
    # whole trailing piece, so chunk two starts with the D sentence again.
    sentences = [letter * 40 for letter in "ABCDEF"]
    blocks = [ExtractedBlock(text=". ".join(sentences))]

    drafts = split_blocks(blocks, chunk_size=200, chunk_overlap=60, separator=". ")

    assert [draft.content for draft in drafts] == [
        ". ".join(letter * 40 for letter in "ABCD") + ".",
        ". ".join(letter * 40 for letter in "DEF"),
    ]


def test_split_falls_back_to_line_boundaries_then_hard_cuts() -> None:
    lines = "\n".join("L" * 90 for _ in range(5))  # no paragraph breaks
    drafts = split_blocks([ExtractedBlock(text=lines)], chunk_size=200, chunk_overlap=0)
    assert all(len(draft.content) <= 200 for draft in drafts)
    assert all("\n" not in draft.content or len(draft.content) <= 200 for draft in drafts)

    single_line = "C" * 900  # no boundaries at all: hard character cuts
    drafts = split_blocks([ExtractedBlock(text=single_line)], chunk_size=400, chunk_overlap=0)
    assert [len(draft.content) for draft in drafts] == [400, 400, 100]
    assert "".join(draft.content for draft in drafts) == single_line


def test_split_overlap_never_emits_a_pure_subset_of_the_previous_chunk() -> None:
    # The oversized B run recurses to character level where the carry-over
    # window retains 100 characters; the final window must still contain new
    # text, never re-emit pure carry-over as its own segment.
    text_value = "A" * 500 + "\n\n" + "B" * 1100
    drafts = split_blocks([ExtractedBlock(text=text_value)], chunk_size=1000, chunk_overlap=100)

    # 100 characters of chunk two carry into chunk three, followed by the
    # 100 remaining new characters; a pure-carry-over chunk would be "B" * 100.
    assert [draft.content for draft in drafts] == [
        "A" * 500,
        "B" * 1000,
        "B" * 200,
    ]


def test_split_chinese_sentences_fall_back_to_the_full_stop_boundary() -> None:
    sentences = ["第一句话内容比较长一些。", "第二句话也有不少内容。", "第三句话继续增加长度。"]
    drafts = split_blocks([ExtractedBlock(text="".join(sentences))], chunk_size=20, chunk_overlap=0)

    assert [draft.content for draft in drafts] == [
        sentences[0],
        sentences[1],
        sentences[2],
    ]


def test_split_child_chunks_merges_pieces_and_hard_cuts_oversized_ones() -> None:
    # Pieces pack up to the child size without overlap; an oversized piece
    # falls through to the fallback boundaries and finally hard cuts.
    text_value = "短句一。短句二。" + "长" * 25 + "。尾句。"
    children = split_child_chunks(text_value, child_chunk_size=10, child_chunk_separator="。")

    assert children[0] == "短句一。短句二。"
    assert all(len(child) <= 10 for child in children)
    assert "".join(children).replace("。", "") == text_value.replace("。", "")


# ---------------------------------------------------------------------------
# Cleaner
# ---------------------------------------------------------------------------


def test_clean_text_url_removal_stops_at_cjk_text() -> None:
    # Chinese prose rarely puts whitespace after a URL; the characters that
    # follow belong to the document and must survive.
    raw = "详见https://x.invalid/page下一节，以及http://y.invalid/z。结束"
    cleaned = clean_text(raw, remove_extra_spaces=False, remove_urls_emails=True)

    assert cleaned == "详见下一节，以及。结束"


# ---------------------------------------------------------------------------
# Chunk preview
# ---------------------------------------------------------------------------


def _preview_request(path: Path, **overrides: object) -> KnowledgeChunkPreviewRequest:
    payload: dict[str, object] = {
        "original_name": path.name,
        "source_path": path,
    }
    payload.update(overrides)
    if "size_bytes" not in payload:
        payload["size_bytes"] = path.stat().st_size
    return KnowledgeChunkPreviewRequest(**payload)  # type: ignore[arg-type]


async def _preview_chunks(
    request: KnowledgeChunkPreviewRequest,
    settings: KnowledgeSettings,
):
    from actweave_knowledge.extraction.registry import default_registry
    from actweave_knowledge.extraction.runtime import ParserSlots
    from actweave_knowledge.ingestion.profiles import build_file_capabilities

    async def guard() -> None:
        return None

    capabilities = build_file_capabilities(settings, default_registry())
    return await preview_document_chunks(
        request,
        settings,
        capability_revision=capabilities.capability_revision,
        parser_slots=ParserSlots(1),
        guard=guard,
        registry=default_registry(),
    )


@pytest.mark.asyncio
async def test_preview_applies_cleaning_rules_and_custom_separator(tmp_path: Path) -> None:
    source = tmp_path / "rules.txt"
    source.write_text("联系 a@b.co 详见 https://x.invalid/page###下一节内容", encoding="utf-8")

    settings = KnowledgeSettings.model_validate({"enabled": False})
    preview = await _preview_chunks(
        _preview_request(source, chunk_size=200, chunk_overlap=0, chunk_separator="###", remove_urls_emails=True),
        settings,
    )

    # The URL (including the ASCII "###" glued to it) is removed, and the
    # remaining sections pack back into a single small chunk.
    assert preview.total == 1
    assert "a@b.co" not in preview.chunks[0].content
    assert "x.invalid" not in preview.chunks[0].content
    assert "下一节内容" in preview.chunks[0].content


@pytest.mark.asyncio
async def test_preview_parent_child_nests_children_and_general_stays_flat(tmp_path: Path) -> None:
    source = tmp_path / "nested.md"
    source.write_text("第一句内容。第二句内容。第三句内容。", encoding="utf-8")
    settings = KnowledgeSettings.model_validate({"enabled": False})

    nested = await _preview_chunks(
        _preview_request(
            source,
            chunk_size=200,
            chunk_overlap=0,
            chunking_mode="parent_child",
            child_chunk_size=100,
            child_chunk_separator="。",
        ),
        settings,
    )
    assert nested.total == 1
    parent = nested.chunks[0]
    assert parent.content == "第一句内容。第二句内容。第三句内容。"
    assert parent.child_contents
    assert all(child in parent.content for child in parent.child_contents)

    flat = await _preview_chunks(_preview_request(source, chunk_size=200, chunk_overlap=0), settings)
    assert flat.chunks[0].child_contents == ()

    with pytest.raises(KnowledgeError) as bad_mode:
        await _preview_chunks(_preview_request(source, chunking_mode="fancy"), settings)
    assert bad_mode.value.code == KNOWLEDGE_INVALID_REQUEST

    with pytest.raises(KnowledgeError) as bad_child:
        await _preview_chunks(
            _preview_request(source, chunk_size=300, chunking_mode="parent_child", child_chunk_size=300),
            settings,
        )
    assert bad_child.value.code == KNOWLEDGE_INVALID_REQUEST


@pytest.mark.asyncio
async def test_preview_rejects_invalid_parameters_and_extensions(tmp_path: Path) -> None:
    source = tmp_path / "preview.txt"
    source.write_text("正文", encoding="utf-8")
    settings = KnowledgeSettings.model_validate({"enabled": False})

    with pytest.raises(KnowledgeError) as invalid_extension:
        await _preview_chunks(_preview_request(tmp_path / "evil.exe", size_bytes=3), settings)
    assert invalid_extension.value.code == KNOWLEDGE_INVALID_REQUEST

    with pytest.raises(KnowledgeError) as invalid_separator:
        await _preview_chunks(_preview_request(source, chunk_separator=""), settings)
    assert invalid_separator.value.code == KNOWLEDGE_INVALID_REQUEST

    with pytest.raises(KnowledgeError) as oversized:
        await _preview_chunks(
            _preview_request(source, size_bytes=settings.upload_max_bytes + 1),
            settings,
        )
    assert oversized.value.code == KNOWLEDGE_INVALID_REQUEST


# ---------------------------------------------------------------------------
# Pipeline harness
# ---------------------------------------------------------------------------


class _FakeIngestStore(ExtractionObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_download = False
        self.on_download = None

    async def download_to(
        self,
        key: str,
        target_path: Path,
        *,
        max_bytes: int | None = None,
    ) -> None:
        if self.fail_download:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储读取失败，请稍后重试")
        if self.on_download is not None:
            await self.on_download()
        await super().download_to(key, target_path, max_bytes=max_bytes)


class _PipelineHarness:
    def __init__(self, engine, factory, store: _FakeIngestStore, client: _FakeModelClient, handler: KnowledgeIngestionHandler) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.store = store
        self.client = client
        self.handler = handler


async def _pipeline_harness(postgres_database_url: str, **settings_overrides: object) -> _PipelineHarness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = KnowledgeSettings.model_validate({"enabled": False, **settings_overrides})
    store = _FakeIngestStore()
    client = _FakeModelClient()
    handler = KnowledgeIngestionHandler(
        session_factory=factory,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        quota=make_test_quota_port(factory),
        model_client=client,  # type: ignore[arg-type]
        model_port=registry_model_port(),
        project_active_check=is_knowledge_project_active,
    )
    return _PipelineHarness(engine, factory, store, client, handler)


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
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"m4_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"m4-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _seed_stack(
    harness: _PipelineHarness,
    *,
    document_status: str = "queued",
    document_version: int = 1,
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    chunk_separator: str = "\\n\\n",
    remove_extra_spaces: bool = False,
    remove_urls_emails: bool = False,
    chunking_mode: str = "general",
    child_chunk_size: int = 500,
    child_chunk_separator: str = "\\n",
    original_name: str = "note.md",
    content: bytes = "# 标题\n\n知识库摄取测试文本。".encode(),
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed project, registry embedding model, base, one document, and its object bytes."""

    profile = ProcessingProfile(
        parse=make_parse_profile(Path(original_name).suffix),
        chunk=make_chunk_profile(
            size=chunk_size,
            overlap=chunk_overlap,
            separator=chunk_separator,
            mode=chunking_mode,
            child_size=child_chunk_size,
            child_separator=child_chunk_separator,
            remove_extra_spaces=remove_extra_spaces,
            remove_urls_emails=remove_urls_emails,
        ),
    )
    provider_id = await seed_provider(harness.factory)
    embedding_model_id = await seed_embedding_model(harness.factory, provider_id, dimension=8)
    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = uuid.uuid4()
        session.add(
            KnowledgeBaseRow(
                id=base_id,
                project_id=project_id,
                name=f"base-{base_id.hex[:8]}",
                embedding_model_id=embedding_model_id,
            )
        )
        await session.flush()
        document_id = uuid.uuid4()
        storage_key = f"projects/{project_id}/knowledge/{base_id}/{document_id}{Path(original_name).suffix}"
        session.add(
            KnowledgeDocumentRow(
                id=document_id,
                project_id=project_id,
                knowledge_base_id=base_id,
                name=original_name,
                original_name=original_name,
                storage_key=storage_key,
                size_bytes=len(content),
                status=document_status,
                version=document_version,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                chunk_separator=chunk_separator,
                remove_extra_spaces=remove_extra_spaces,
                remove_urls_emails=remove_urls_emails,
                chunking_mode=chunking_mode,
                child_chunk_size=child_chunk_size,
                child_chunk_separator=child_chunk_separator,
                source_sha256=hashlib.sha256(content).hexdigest(),
                parsing_profile=profile.model_dump(mode="json"),
                capability_revision="a" * 64,
            )
        )
    harness.store.objects[storage_key] = content
    return project_id, base_id, document_id


async def _queue_ingest_task(
    harness: _PipelineHarness,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    target_version: int = 1,
) -> uuid.UUID:
    task_id = uuid.uuid4()
    async with harness.factory() as session, session.begin():
        session.add(
            KnowledgeTaskRow(
                id=task_id,
                project_id=project_id,
                resource_id=document_id,
                kind="ingest_document",
                target_version=target_version,
                status="queued",
            )
        )
    return task_id


async def _claim(harness: _PipelineHarness) -> KnowledgeTaskClaim:
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
            reparse_settings=row.reparse_settings,
        )


async def _document_row(harness: _PipelineHarness, document_id: uuid.UUID) -> KnowledgeDocumentRow:
    async with harness.factory() as session:
        row = await session.get(KnowledgeDocumentRow, document_id)
        assert row is not None
        return row


async def _task_row(harness: _PipelineHarness, task_id: uuid.UUID) -> KnowledgeTaskRow:
    async with harness.factory() as session:
        row = await session.get(KnowledgeTaskRow, task_id)
        assert row is not None
        return row


async def _segment_rows(harness: _PipelineHarness, document_id: uuid.UUID) -> list[KnowledgeSegmentRow]:
    async with harness.factory() as session:
        rows = await session.scalars(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document_id).order_by(KnowledgeSegmentRow.position))
        return list(rows.all())


async def _child_rows(harness: _PipelineHarness, document_id: uuid.UUID) -> list[KnowledgeSegmentChildRow]:
    async with harness.factory() as session:
        rows = await session.scalars(select(KnowledgeSegmentChildRow).where(KnowledgeSegmentChildRow.knowledge_document_id == document_id).order_by(KnowledgeSegmentChildRow.knowledge_segment_id, KnowledgeSegmentChildRow.position))
        return list(rows.all())


@pytest.fixture
def temp_dir_tracker(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every ingest temp directory so cleanup can be asserted."""

    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def _tracking_mkdtemp(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", _tracking_mkdtemp)
    return created


# ---------------------------------------------------------------------------
# Pipeline behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_processes_queued_document_to_ready(postgres_database_url: str, temp_dir_tracker: list[Path]) -> None:
    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, _, document_id = await _seed_stack(harness)
        task_id = await _queue_ingest_task(harness, project_id, document_id)
        claim = await _claim(harness)

        await harness.handler(claim)

        document = await _document_row(harness, document_id)
        assert document.status == "ready"
        assert document.error_message is None
        segments = await _segment_rows(harness, document_id)
        assert document.segment_count == len(segments) > 0
        assert [segment.position for segment in segments] == list(range(1, len(segments) + 1))
        assert all(segment.document_version == 1 for segment in segments)
        assert all(len(list(segment.embedding)) == 8 for segment in segments)
        # K1: per-segment character counts, aggregated onto the document.
        assert all(segment.word_count == len(segment.content) for segment in segments)
        assert all(segment.enabled is True for segment in segments)
        assert document.word_count == sum(len(segment.content) for segment in segments)
        assert harness.client.calls == [[segment.index_text for segment in segments]]

        task = await _task_row(harness, task_id)
        assert task.status == "succeeded"
        assert task.claim_token is None and task.lease_until is None
        assert task.finished_at is not None

        assert temp_dir_tracker and not any(path.exists() for path in temp_dir_tracker)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_honors_frozen_separator_and_cleaning_rules(postgres_database_url: str) -> None:
    harness = await _pipeline_harness(postgres_database_url)
    try:
        content = "第一节 含链接 https://example.invalid/page 与邮箱 a@b.co###第二节   多余空格###第三节正文"
        project_id, _, document_id = await _seed_stack(
            harness,
            original_name="rules.txt",
            content=content.encode(),
            chunk_size=200,
            chunk_overlap=0,
            chunk_separator="###",
            remove_extra_spaces=True,
            remove_urls_emails=True,
        )
        await _queue_ingest_task(harness, project_id, document_id)

        await harness.handler(await _claim(harness))

        document = await _document_row(harness, document_id)
        assert document.status == "ready"
        segments = await _segment_rows(harness, document_id)
        combined = "".join(segment.content for segment in segments)
        assert "example.invalid" not in combined
        assert "a@b.co" not in combined
        assert "  " not in combined  # extra spaces compressed
        assert "第三节正文" in combined
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_preview_chunks_match_ingested_segments_exactly(postgres_database_url: str, tmp_path: Path) -> None:
    """K2 acceptance: identical parameters make preview and ingestion agree."""

    harness = await _pipeline_harness(postgres_database_url)
    try:
        paragraphs = [f"第{index}段。" + "正文内容" * 90 + " 空格  与链接 https://example.invalid/x" for index in range(1, 7)]
        content = "\n\n".join(paragraphs)
        parameters = {
            "chunk_size": 300,
            "chunk_overlap": 50,
            "chunk_separator": "\\n\\n",
            "remove_extra_spaces": True,
            "remove_urls_emails": True,
        }
        project_id, _, document_id = await _seed_stack(
            harness,
            original_name="parity.md",
            content=content.encode(),
            **parameters,  # type: ignore[arg-type]
        )
        await _queue_ingest_task(harness, project_id, document_id)
        await harness.handler(await _claim(harness))

        document = await _document_row(harness, document_id)
        assert document.status == "ready"
        segments = await _segment_rows(harness, document_id)

        source = tmp_path / "parity.md"
        source.write_text(content, encoding="utf-8")
        settings = KnowledgeSettings.model_validate({"enabled": False})
        preview = await _preview_chunks(
            KnowledgeChunkPreviewRequest(
                original_name="parity.md",
                source_path=source,
                size_bytes=source.stat().st_size,
                **parameters,  # type: ignore[arg-type]
            ),
            settings,
        )

        assert preview.total == len(segments)
        assert [chunk.content for chunk in preview.chunks] == [segment.content for segment in segments[: len(preview.chunks)]]
        assert [chunk.word_count for chunk in preview.chunks] == [segment.word_count for segment in segments[: len(preview.chunks)]]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_republish_replaces_previous_segments(postgres_database_url: str) -> None:
    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, base_id, document_id = await _seed_stack(harness, content="新版本的内容。".encode())
        # Old segments from a previous version must disappear on publish.
        async with harness.factory() as session, session.begin():
            session.add(
                KnowledgeSegmentRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    knowledge_document_id=document_id,
                    document_version=1,
                    position=1,
                    content="旧版本分段",
                    source_position={},
                    embedding=[0.5] * 8,
                )
            )
            document = await session.get(KnowledgeDocumentRow, document_id)
            assert document is not None
            document.version = 2
        await _queue_ingest_task(harness, project_id, document_id, target_version=2)
        claim = await _claim(harness)

        await harness.handler(claim)

        segments = await _segment_rows(harness, document_id)
        assert all(segment.document_version == 2 for segment in segments)
        assert all(segment.content != "旧版本分段" for segment in segments)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_parent_child_ingest_embeds_children_and_leaves_parents_unvectored(postgres_database_url: str) -> None:
    """K3: parent_child publishes NULL-embedding parents plus vectored child rows."""

    harness = await _pipeline_harness(postgres_database_url)
    try:
        first_paragraph = "".join(f"第一段第{index}句甲乙丙丁。" for index in range(1, 16))
        second_paragraph = "".join(f"第二段第{index}句甲乙丙丁。" for index in range(1, 16))
        content = f"{first_paragraph}\n\n{second_paragraph}".encode()
        project_id, base_id, document_id = await _seed_stack(
            harness,
            chunking_mode="parent_child",
            chunk_size=200,
            chunk_overlap=0,
            child_chunk_size=100,
            child_chunk_separator="。",
            content=content,
        )
        task_id = await _queue_ingest_task(harness, project_id, document_id)

        await harness.handler(await _claim(harness))

        document = await _document_row(harness, document_id)
        assert document.status == "ready"
        segments = await _segment_rows(harness, document_id)
        assert segments
        assert all(segment.embedding is None for segment in segments)
        assert document.segment_count == len(segments)
        assert document.word_count == sum(len(segment.content) for segment in segments)

        children = await _child_rows(harness, document_id)
        assert len(children) >= 3
        by_parent: dict[uuid.UUID, list[KnowledgeSegmentChildRow]] = {}
        for child in children:
            by_parent.setdefault(child.knowledge_segment_id, []).append(child)
        assert set(by_parent) == {segment.id for segment in segments}
        for segment in segments:
            group = by_parent[segment.id]
            assert [child.position for child in group] == list(range(1, len(group) + 1))
            # Second-stage splitting only slices parent text, never invents it.
            assert all(child.content in segment.content for child in group)
            assert all(child.document_version == segment.document_version for child in group)
            assert all(len(list(child.embedding)) == 8 for child in group)
            assert all(child.word_count == len(child.content) for child in group)
        # Exactly one embed call, covering child contents only (never parents),
        # flattened in parent-position order.
        assert harness.client.calls == [[child.index_text for segment in segments for child in by_parent[segment.id]]]

        task = await _task_row(harness, task_id)
        assert task.status == "succeeded"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_publish_maintains_lexical_fields_on_parents_and_children(postgres_database_url: str) -> None:
    """T8: the publish transaction derives lexical_v1 tokens for every row.

    Both chunking modes maintain the parent field (fusion scores shortlisted
    parents), and parent_child rows additionally carry child tokens for the
    lexical recall route.
    """

    harness = await _pipeline_harness(postgres_database_url)
    try:
        first_paragraph = "".join(f"网络配置第{index}句甲乙丙丁。" for index in range(1, 16))
        second_paragraph = "".join(f"存储升级第{index}句甲乙丙丁。" for index in range(1, 16))
        content = f"{first_paragraph}\n\n{second_paragraph}".encode()
        project_id, base_id, document_id = await _seed_stack(
            harness,
            chunking_mode="parent_child",
            chunk_size=200,
            chunk_overlap=0,
            child_chunk_size=100,
            child_chunk_separator="。",
            content=content,
        )
        await _queue_ingest_task(harness, project_id, document_id)

        await harness.handler(await _claim(harness))

        segments = await _segment_rows(harness, document_id)
        children = await _child_rows(harness, document_id)
        assert segments and children
        rows = [("knowledge_segments", row) for row in segments] + [("knowledge_segment_children", row) for row in children]
        async with harness.factory() as session:
            for table, row in rows:
                own_token = encode_lexical_token(lexical_v1_tokens(row.content)[0])
                lexical_version, matches_own, matches_foreign = (
                    await session.execute(
                        text(
                            f"""SELECT lexical_version,
                                       lexical_tsv @@ to_tsquery('simple', :own),
                                       lexical_tsv @@ to_tsquery('simple', :foreign)
                                FROM {table} WHERE id = :id"""
                        ),
                        {
                            "own": own_token,
                            "foreign": encode_lexical_token("不存在的词元"),
                            "id": row.id,
                        },
                    )
                ).one()
                assert lexical_version == KNOWLEDGE_LEXICAL_VERSION
                assert matches_own is True
                assert matches_foreign is False
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_parent_child_republish_replaces_previous_children(postgres_database_url: str) -> None:
    """Re-ingesting a parent_child document must not leak old child rows."""

    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, base_id, document_id = await _seed_stack(
            harness,
            chunking_mode="parent_child",
            content="新版本正文。".encode(),
        )
        # Simulate a previous version's parent + child that must disappear.
        async with harness.factory() as session, session.begin():
            old_segment_id = uuid.uuid4()
            session.add(
                KnowledgeSegmentRow(
                    id=old_segment_id,
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    knowledge_document_id=document_id,
                    document_version=1,
                    position=1,
                    content="旧父块",
                    source_position={},
                    embedding=None,
                )
            )
            await session.flush()
            session.add(
                KnowledgeSegmentChildRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    knowledge_document_id=document_id,
                    knowledge_segment_id=old_segment_id,
                    document_version=1,
                    position=1,
                    content="旧子块",
                    embedding=[0.5] * 8,
                )
            )
            document = await session.get(KnowledgeDocumentRow, document_id)
            assert document is not None
            document.version = 2
        await _queue_ingest_task(harness, project_id, document_id, target_version=2)

        await harness.handler(await _claim(harness))

        segments = await _segment_rows(harness, document_id)
        assert segments and all(segment.document_version == 2 for segment in segments)
        children = await _child_rows(harness, document_id)
        assert children and all(child.content != "旧子块" for child in children)
        assert {child.knowledge_segment_id for child in children} <= {segment.id for segment in segments}
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_is_noop_for_missing_deleting_or_mismatched_documents(postgres_database_url: str) -> None:
    harness = await _pipeline_harness(postgres_database_url)
    try:
        # Version mismatch: task targets version 1, document is already at 2.
        project_id, _, document_id = await _seed_stack(harness, document_version=2)
        await _queue_ingest_task(harness, project_id, document_id, target_version=1)
        await harness.handler(await _claim(harness))
        document = await _document_row(harness, document_id)
        assert document.status == "queued"
        assert await _segment_rows(harness, document_id) == []

        # Deleting document: never processed.
        project_id, _, deleting_id = await _seed_stack(harness, document_status="deleting")
        await _queue_ingest_task(harness, project_id, deleting_id)
        await harness.handler(await _claim(harness))
        assert (await _document_row(harness, deleting_id)).status == "deleting"

        # Missing document: the row was deleted after the task was queued.
        project_id, _, missing_id = await _seed_stack(harness)
        await _queue_ingest_task(harness, project_id, missing_id)
        async with harness.factory() as session, session.begin():
            row = await session.get(KnowledgeDocumentRow, missing_id)
            assert row is not None
            await session.delete(row)
        await harness.handler(await _claim(harness))  # must not raise
        assert harness.client.calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_late_result_is_not_published_after_midflight_delete(postgres_database_url: str, temp_dir_tracker: list[Path]) -> None:
    """A document deleted mid-processing settles the claim as a succeeded no-op."""

    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, _, document_id = await _seed_stack(harness)
        task_id = await _queue_ingest_task(harness, project_id, document_id)
        claim = await _claim(harness)

        async def _delete_midflight() -> None:
            async with harness.factory() as session, session.begin():
                row = await session.get(KnowledgeDocumentRow, document_id)
                assert row is not None
                row.status = "deleting"
                row.version = row.version + 1
                row.error_message = None

        harness.store.on_download = _delete_midflight

        with pytest.raises(KnowledgeError) as stale:
            await harness.handler(claim)
        assert stale.value.code == KNOWLEDGE_CONFLICT

        document = await _document_row(harness, document_id)
        assert document.status == "deleting"
        assert await _segment_rows(harness, document_id) == []
        task = await _task_row(harness, task_id)
        assert task.status == "running"  # Worker settlement owns this stale claim.
        assert temp_dir_tracker and not any(path.exists() for path in temp_dir_tracker)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_aborts_when_token_segments_exceed_document_quota(postgres_database_url: str) -> None:
    """The P3 splitter enforces the configured parent-row quota before Provider use."""

    harness = await _pipeline_harness(postgres_database_url, max_segments_per_document=1)
    try:
        long_text = ("段落甲" * 200 + "\n\n" + "段落乙" * 200).encode()
        project_id, _, document_id = await _seed_stack(harness, content=long_text, chunk_size=200, chunk_overlap=0)
        await _queue_ingest_task(harness, project_id, document_id)
        claim = await _claim(harness)

        with pytest.raises(KnowledgeError) as error:
            await harness.handler(claim)
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert "切分产生" in error.value.message

        # The failure leaves the document processing; settlement is the
        # worker's responsibility and is covered by the task worker tests.
        assert (await _document_row(harness, document_id)).status == "processing"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_fails_when_segment_count_exceeds_quota(postgres_database_url: str) -> None:
    """Overlap re-emits characters, so the count gate can trip within budget."""

    harness = await _pipeline_harness(postgres_database_url, max_segments_per_document=2)
    try:
        long_text = ("甲" * 380).encode()  # 380 chars <= budget 400, but 4 segments
        project_id, _, document_id = await _seed_stack(harness, content=long_text, chunk_size=200, chunk_overlap=100)
        await _queue_ingest_task(harness, project_id, document_id)
        claim = await _claim(harness)

        with pytest.raises(KnowledgeError) as error:
            await harness.handler(claim)
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert "上限 2" in error.value.message
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_parent_child_ingest_rejects_excess_child_vectors_before_embedding(postgres_database_url: str) -> None:
    """Parent count can fit while the vector-carrying child count exceeds quota."""

    harness = await _pipeline_harness(postgres_database_url, max_segments_per_document=2)
    try:
        project_id, _, document_id = await _seed_stack(
            harness,
            content=("甲" * 250).encode(),
            chunking_mode="parent_child",
            chunk_size=400,
            chunk_overlap=0,
            child_chunk_size=100,
        )
        await _queue_ingest_task(harness, project_id, document_id)

        with pytest.raises(KnowledgeError) as error:
            await harness.handler(await _claim(harness))
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert "子块" in error.value.message
        assert "上限 2" in error.value.message
        assert harness.client.calls == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_refuses_a_disabled_embedding_model(postgres_database_url: str) -> None:
    """Disabling a registry model halts provider usage for queued/retried ingests."""

    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, base_id, document_id = await _seed_stack(harness)
        await _queue_ingest_task(harness, project_id, document_id)
        async with harness.factory() as session, session.begin():
            embedding_model_id = await session.scalar(select(KnowledgeBaseRow.embedding_model_id).where(KnowledgeBaseRow.id == base_id))
            model = await session.get(ModelProviderModelRow, embedding_model_id)
            assert model is not None
            model.status = "disabled"
        claim = await _claim(harness)

        with pytest.raises(KnowledgeError) as error:
            await harness.handler(claim)
        assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
        assert harness.client.calls == []
        # The transaction rolled back, so the document is still queued and a
        # re-enabled model plus retry resumes normally.
        assert (await _document_row(harness, document_id)).status == "queued"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_parse_failure_surfaces_parse_failed(postgres_database_url: str, temp_dir_tracker: list[Path]) -> None:
    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, _, document_id = await _seed_stack(harness, original_name="broken.pdf", content=b"not a real pdf")
        await _queue_ingest_task(harness, project_id, document_id)
        claim = await _claim(harness)

        with pytest.raises(KnowledgeError) as error:
            await harness.handler(claim)
        assert error.value.code == KNOWLEDGE_PARSE_FAILED
        assert temp_dir_tracker and not any(path.exists() for path in temp_dir_tracker)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_download_failure_surfaces_storage_unavailable(postgres_database_url: str) -> None:
    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, _, document_id = await _seed_stack(harness)
        await _queue_ingest_task(harness, project_id, document_id)
        harness.store.fail_download = True
        claim = await _claim(harness)

        with pytest.raises(KnowledgeError) as error:
            await harness.handler(claim)
        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_publish_rolls_back_whole_transaction_on_constraint_violation(postgres_database_url: str) -> None:
    """A failed segment insert leaves the document, task, and segments untouched."""

    harness = await _pipeline_harness(postgres_database_url)
    harness.client.dimension = 0  # empty vectors violate the embedding constraint
    try:
        project_id, _, document_id = await _seed_stack(harness)
        task_id = await _queue_ingest_task(harness, project_id, document_id)
        claim = await _claim(harness)

        with pytest.raises(KnowledgeError) as error:
            await harness.handler(claim)
        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE

        document = await _document_row(harness, document_id)
        assert document.status == "processing"  # publish rolled back entirely
        assert document.segment_count == 0
        assert await _segment_rows(harness, document_id) == []
        assert (await _task_row(harness, task_id)).status == "running"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_ingest_cancellation_still_cleans_the_temp_directory(postgres_database_url: str, temp_dir_tracker: list[Path]) -> None:
    harness = await _pipeline_harness(postgres_database_url)
    harness.client.blocker = asyncio.Event()  # embed hangs until cancelled
    try:
        project_id, _, document_id = await _seed_stack(harness)
        await _queue_ingest_task(harness, project_id, document_id)
        claim = await _claim(harness)

        run = asyncio.create_task(harness.handler(claim))
        await asyncio.wait_for(harness.client.started.wait(), timeout=10)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

        assert temp_dir_tracker and not any(path.exists() for path in temp_dir_tracker)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_worker_timeout_waits_for_blocking_parser_before_retry_and_cleanup(
    postgres_database_url: str,
    temp_dir_tracker: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out Knowledge Task must not orphan its parser operation.

    The task remains claimed, and its temporary source remains available,
    until the already-started isolated parser has settled. Only then may
    cancellation cleanup and retry settlement finish.
    """

    harness = await _pipeline_harness(postgres_database_url)
    parser_started = asyncio.Event()
    release_parser = asyncio.Event()
    parser_calls = 0
    active_parsers = 0
    max_active_parsers = 0

    async def _blocking_extraction(*args: object, **kwargs: object):  # noqa: ANN202
        nonlocal parser_calls, active_parsers, max_active_parsers
        del args, kwargs
        parser_calls += 1
        active_parsers += 1
        max_active_parsers = max(max_active_parsers, active_parsers)
        parser_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_parser.wait()
            raise
        finally:
            active_parsers -= 1

    monkeypatch.setattr(pipeline_module, "run_extraction", _blocking_extraction)
    stop_event = asyncio.Event()
    run: asyncio.Task[None] | None = None
    try:
        project_id, _, document_id = await _seed_stack(harness)
        task_id = await _queue_ingest_task(harness, project_id, document_id)

        async def _project_active(
            session: AsyncSession,
            claimed_project_id: uuid.UUID,
        ) -> bool:
            del session
            return claimed_project_id == project_id

        worker = KnowledgeTaskWorker(
            session_factory=harness.factory,
            handlers={"ingest_document": harness.handler},
            project_active_check=_project_active,
            concurrency=1,
            task_timeout_seconds=1,
            poll_interval_seconds=0.05,
            retry_delay_seconds=60,
        )
        run = asyncio.create_task(worker.run(stop_event))

        async with asyncio.timeout(5):
            while not parser_started.is_set():
                await asyncio.sleep(0.01)
        await asyncio.sleep(1.1)

        during_timeout = await _task_row(harness, task_id)
        assert during_timeout.status == "running"
        assert during_timeout.attempt_count == 1
        assert parser_calls == 1
        assert max_active_parsers == 1
        assert temp_dir_tracker and all(path.exists() for path in temp_dir_tracker)

        release_parser.set()
        async with asyncio.timeout(5):
            while (await _task_row(harness, task_id)).status != "retry_wait":
                await asyncio.sleep(0.01)

        settled = await _task_row(harness, task_id)
        assert settled.attempt_count == 1
        assert "超过 1 秒" in (settled.error_message or "")
        assert active_parsers == 0
        assert not any(path.exists() for path in temp_dir_tracker)
    finally:
        release_parser.set()
        stop_event.set()
        if run is not None:
            await asyncio.wait_for(run, timeout=10)
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Explicit re-parse of the original file (T3)
# ---------------------------------------------------------------------------


def _reparse_documents_service(harness: _PipelineHarness) -> KnowledgeDocumentService:
    return KnowledgeDocumentService(
        project_active_check=is_knowledge_project_active,
        session_factory=harness.factory,
        settings=KnowledgeSettings.model_validate({"enabled": False}),
        file_capabilities=make_test_file_capability_provider(),
        object_store=harness.store,  # type: ignore[arg-type]
        quota=make_test_quota_port(harness.factory),
    )


@pytest.mark.asyncio
async def test_reparse_preview_matches_publish_and_freezes_parameters(postgres_database_url: str) -> None:
    """The preview computed from the stored original equals the published rows,
    manual text is overwritten only by this explicit operation, and the
    document's stored parameters swap to the confirmed ones only on publish."""

    harness = await _pipeline_harness(postgres_database_url)
    try:
        paragraphs = [f"第{index}段。" + "正文内容" * 60 for index in range(1, 5)]
        content = "\n\n".join(paragraphs)
        project_id, _, document_id = await _seed_stack(
            harness,
            original_name="reparse.md",
            content=content.encode(),
            chunk_size=1000,
            chunk_overlap=100,
        )
        await _queue_ingest_task(harness, project_id, document_id)
        await harness.handler(await _claim(harness))
        # A manual edit that an explicit re-parse is allowed to overwrite.
        async with harness.factory() as session, session.begin():
            first = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document_id).order_by(KnowledgeSegmentRow.position))
            assert first is not None
            first.content = "人工修改过的内容"

        documents = _reparse_documents_service(harness)
        request = KnowledgeReparseRequest(
            expected_version=1,
            chunk_size=300,
            chunk_overlap=0,
            chunk_separator="\\n\\n",
        )
        previewed = await documents.preview_reparse(project_id, document_id, request)
        assert previewed.document_version == 1
        assert previewed.preview.total > 0

        view = await documents.reparse_document(project_id, document_id, request)
        assert view.status == "queued"
        assert view.version == 2
        # The stored parameters stay the old ones until the publish succeeds.
        assert view.chunk_size == 1000
        assert view.content_initialized is True

        task = await _open_indexing_task(harness, document_id)
        assert task.kind == "ingest_document"
        assert task.target_version == 2
        assert {key: value for key, value in task.reparse_settings.items() if key not in {"processing_profile", "capability_revision"}} == {
            "chunk_size": 300,
            "chunk_overlap": 0,
            "chunk_separator": "\\n\\n",
            "remove_extra_spaces": False,
            "remove_urls_emails": False,
            "chunking_mode": "general",
            "child_chunk_size": 500,
            "child_chunk_separator": "\\n",
        }

        from actweave_knowledge.persistence.tasks import validated_reparse_settings

        assert validated_reparse_settings(task.reparse_settings) == task.reparse_settings
        assert task.reparse_settings["processing_profile"]["chunk"]["unit"] == "token"
        assert len(task.reparse_settings["capability_revision"]) == 64

        await harness.handler(await _claim(harness))

        document = await _document_row(harness, document_id)
        assert document.status == "ready"
        assert document.version == 2
        assert document.published_version == 2
        # Publish swaps the stored parameters together with the rows.
        assert document.chunk_size == 300
        assert document.chunk_overlap == 0
        segments = await _segment_rows(harness, document_id)
        assert all(segment.document_version == 2 for segment in segments)
        assert "人工修改过的内容" not in [segment.content for segment in segments]
        assert [chunk.content for chunk in previewed.preview.chunks] == [segment.content for segment in segments[: len(previewed.preview.chunks)]]
        assert previewed.preview.total == len(segments)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reparse_admission_rejects_cas_status_open_tasks_and_bad_parameters(postgres_database_url: str) -> None:
    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, _, document_id = await _seed_stack(harness)
        await _queue_ingest_task(harness, project_id, document_id)
        await harness.handler(await _claim(harness))
        documents = _reparse_documents_service(harness)

        # Stale expected_version is a CAS conflict.
        with pytest.raises(KnowledgeError) as stale:
            await documents.reparse_document(project_id, document_id, KnowledgeReparseRequest(expected_version=7))
        assert stale.value.code == KNOWLEDGE_CONFLICT

        # Invalid frozen parameters are rejected before anything changes.
        with pytest.raises(KnowledgeError) as bad:
            await documents.reparse_document(
                project_id,
                document_id,
                KnowledgeReparseRequest(expected_version=1, chunk_size=1),
            )
        assert bad.value.code == KNOWLEDGE_INVALID_REQUEST

        # An open indexing task owns the slot; admission must reject.
        async with harness.factory() as session, session.begin():
            session.add(
                KnowledgeTaskRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    resource_id=document_id,
                    kind="reembed_document",
                    target_version=1,
                    status="retry_wait",
                    attempt_count=1,
                )
            )
        with pytest.raises(KnowledgeError) as open_task:
            await documents.reparse_document(project_id, document_id, KnowledgeReparseRequest(expected_version=1))
        assert open_task.value.code == KNOWLEDGE_INVALID_REQUEST
        async with harness.factory() as session, session.begin():
            await session.execute(text("DELETE FROM knowledge_tasks WHERE resource_id = :rid AND status = 'retry_wait'"), {"rid": str(document_id)})

        # Processing/deleting documents reject the operation outright.
        for status in ("processing", "deleting"):
            async with harness.factory() as session, session.begin():
                row = await session.get(KnowledgeDocumentRow, document_id)
                assert row is not None
                row.status = status
            with pytest.raises(KnowledgeError) as blocked:
                await documents.reparse_document(project_id, document_id, KnowledgeReparseRequest(expected_version=1))
            assert blocked.value.code == KNOWLEDGE_INVALID_REQUEST, status
        async with harness.factory() as session, session.begin():
            row = await session.get(KnowledgeDocumentRow, document_id)
            assert row is not None
            row.status = "ready"

        # A non-active base stops re-parsing, like retry.
        async with harness.factory() as session, session.begin():
            base = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
            assert base is not None
            base.status = "disabled"
        with pytest.raises(KnowledgeError) as inactive:
            await documents.reparse_document(project_id, document_id, KnowledgeReparseRequest(expected_version=1))
        assert inactive.value.code == KNOWLEDGE_INVALID_REQUEST

        # Nothing changed: same version, no new tasks, rows untouched.
        document = await _document_row(harness, document_id)
        assert (document.status, document.version) == ("ready", 1)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reparse_failure_keeps_old_rows_parameters_and_projection(postgres_database_url: str) -> None:
    """A finally-failed re-parse keeps the published rows, the old stored
    parameters, and the old published_version; the maintenance listing keeps
    showing the residual old content instead of an empty page."""

    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, _, document_id = await _seed_stack(harness, content="旧版本正文内容。".encode())
        await _queue_ingest_task(harness, project_id, document_id)
        await harness.handler(await _claim(harness))
        old_segments = await _segment_rows(harness, document_id)
        assert len(old_segments) == 1

        documents = _reparse_documents_service(harness)
        await documents.reparse_document(
            project_id,
            document_id,
            KnowledgeReparseRequest(expected_version=1, chunk_size=300),
        )
        harness.client.fail = True
        while True:
            claim = await _claim(harness)
            with pytest.raises(KnowledgeError):
                await harness.handler(claim)
            async with harness.factory() as session, session.begin():
                from actweave_knowledge.persistence.tasks import settle_task_failure

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
        assert document.version == 2
        assert document.published_version == 1
        # The new parameters never came to explain the old rows.
        assert document.chunk_size == 1000
        assert document.segment_count == 1
        [row] = await _segment_rows(harness, document_id)
        assert row.content == "旧版本正文内容。"
        assert row.document_version == 1

        # The read-only maintenance projection still lists the residual rows.
        views, total = await documents.list_document_segments(project_id, document_id)
        assert total == 1
        assert [view.content for view in views] == ["旧版本正文内容。"]
        assert views[0].document_version == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reparse_retry_inherits_frozen_settings_and_keeps_counters(postgres_database_url: str) -> None:
    harness = await _pipeline_harness(postgres_database_url)
    try:
        project_id, _, document_id = await _seed_stack(harness, content=("第一段。" + "长内容" * 120 + "\n\n第二段。" + "更多内容" * 120).encode())
        await _queue_ingest_task(harness, project_id, document_id)
        await harness.handler(await _claim(harness))

        documents = _reparse_documents_service(harness)
        await documents.reparse_document(
            project_id,
            document_id,
            KnowledgeReparseRequest(expected_version=1, chunk_size=300, chunk_overlap=0),
        )
        harness.client.fail = True
        while True:
            claim = await _claim(harness)
            with pytest.raises(KnowledgeError):
                await harness.handler(claim)
            async with harness.factory() as session, session.begin():
                from actweave_knowledge.persistence.tasks import settle_task_failure

                outcome = await settle_task_failure(
                    session,
                    claim.id,
                    claim.claim_token,
                    error_message="Embedding 调用失败",
                    retry_delay_seconds=0,
                )
            if outcome == "failed":
                break

        failed = await _document_row(harness, document_id)
        # Old published rows survive, so the counters keep describing them.
        assert failed.segment_count > 0
        old_word_count = failed.word_count

        retried = await documents.retry_document(project_id, document_id)
        assert retried.status == "queued"
        assert retried.segment_count == failed.segment_count
        assert retried.word_count == old_word_count

        task = await _open_indexing_task(harness, document_id)
        assert task.kind == "ingest_document"
        assert task.target_version == 3
        assert task.reparse_settings is not None
        assert task.reparse_settings["chunk_size"] == 300

        harness.client.fail = False
        await harness.handler(await _claim(harness))

        document = await _document_row(harness, document_id)
        assert document.status == "ready"
        assert document.published_version == 3
        # The retried publish applies the inherited frozen parameters.
        assert document.chunk_size == 300
        segments = await _segment_rows(harness, document_id)
        assert all(segment.document_version == 3 for segment in segments)
        assert all(segment.token_count <= 300 for segment in segments)
    finally:
        await harness.engine.dispose()


async def _open_indexing_task(harness: _PipelineHarness, document_id: uuid.UUID) -> KnowledgeTaskRow:
    async with harness.factory() as session:
        row = await session.scalar(
            select(KnowledgeTaskRow)
            .where(
                KnowledgeTaskRow.resource_id == document_id,
                KnowledgeTaskRow.status.in_(("queued", "running", "retry_wait")),
            )
            .order_by(KnowledgeTaskRow.created_at.desc())
        )
        assert row is not None, "expected an open indexing task"
        return row
