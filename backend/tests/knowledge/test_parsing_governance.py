"""P3 manual-governance and stored model-text compatibility contracts."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from actweave_knowledge import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_LEXICAL_VERSION,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeError,
    KnowledgeSegmentCreate,
    KnowledgeSegmentUpdate,
)
from actweave_knowledge.extraction.contracts import ExtractionError, ProcessingProfile
from actweave_knowledge.ingestion.index_text import build_index_text
from actweave_knowledge.ingestion.tokenizer import count_knowledge_tokens
from actweave_knowledge.persistence.derivations import stored_model_text
from actweave_knowledge.persistence.models import (
    KnowledgeAttachmentRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentAttachmentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
)
from actweave_knowledge.retrieval import lexical_index_input
from actweave_knowledge.segments.service import KnowledgeSegmentService, _manual_derivation
from ingestion_test_helpers import FakeModelClient, ingestion_harness
from parsing_test_helpers import (
    make_chunk_profile,
    make_parse_profile,
    write_docx_with_image,
)
from sqlalchemy import func, select


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["splitter_version", "cleaner_version"])
async def test_manual_old_token_profile_rejects_before_model_dispatch(field: str) -> None:
    """A stale token splitter must not send a manual child derivation to a model."""

    chunk = make_chunk_profile(mode="parent_child", size=200, child_size=100, overlap=0)
    profile = ProcessingProfile(
        parse=make_parse_profile(".md"),
        chunk=chunk.model_copy(update={field: "old-unavailable-build"}),
    )
    document = SimpleNamespace(
        chunking_mode="parent_child",
        child_chunk_size=100,
        child_chunk_separator="\n\n",
        parsing_profile=profile.model_dump(mode="json"),
    )
    client = FakeModelClient()
    service = SimpleNamespace(_client=client)

    with pytest.raises(ExtractionError) as error:
        await KnowledgeSegmentService._embed_for_document(
            service,
            document,
            "alpha beta gamma",
            None,
            available_vector_entries=5000,
            authority=None,
        )

    assert error.value.reason_code == "PROCESSING_PROFILE_UNAVAILABLE"
    assert client.calls == []


@pytest.mark.parametrize(
    ("mode", "unit"),
    [("general", "token"), ("parent_child", "character"), ("parent_child", None)],
)
def test_manual_non_resplitting_and_character_paths_keep_their_contract(
    mode: str,
    unit: str | None,
) -> None:
    """Only stale token re-splitting is rejected; historic compatible paths remain usable."""

    profile = (
        None
        if unit is None
        else ProcessingProfile(
            parse=make_parse_profile(".md"),
            chunk=make_chunk_profile(
                unit=unit,
                mode=mode,
                size=200,
                child_size=100,
                overlap=0,
                splitter_version="historical-version",
            ),
        ).model_dump(mode="json")
    )
    document = SimpleNamespace(
        chunking_mode=mode,
        child_chunk_size=100,
        child_chunk_separator="\n\n",
        parsing_profile=profile,
    )
    content = "alpha beta gamma"

    index_text, token_count, children = _manual_derivation(document, content)

    assert index_text == content
    assert token_count == count_knowledge_tokens(content)
    assert bool(children) == (mode == "parent_child")
    assert all(child.source_spans == () for child in children)


def _profile(*, unit: str) -> dict[str, object]:
    return {
        "parse": make_parse_profile(".md").model_dump(mode="json"),
        "chunk": make_chunk_profile(unit=unit).model_dump(mode="json"),
    }


def test_stored_model_text_uses_index_text_and_only_adapts_legacy_rows() -> None:
    markdown = "# 手册\n\n接口为 `List<int>`。"
    assert (
        stored_model_text(
            content=markdown,
            index_text="已保存的模型文本",
            parsing_profile=_profile(unit="token"),
        )
        == "已保存的模型文本"
    )

    expected = "手册\n接口为 List<int>。"
    assert stored_model_text(content=markdown, index_text="", parsing_profile=None) == expected
    assert (
        stored_model_text(
            content=markdown,
            index_text="",
            parsing_profile=_profile(unit="character"),
        )
        == expected
    )

    with pytest.raises(KnowledgeError) as error:
        stored_model_text(
            content=markdown,
            index_text="",
            parsing_profile=_profile(unit="token"),
        )
    assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE


@pytest.mark.asyncio
async def test_manual_update_and_create_publish_all_text_derivations(
    postgres_database_url: str,
    tmp_path,
) -> None:
    source = tmp_path / "guide.md"
    source.write_text("# 原手册\n\n原始段落。", encoding="utf-8")
    profile = ProcessingProfile(
        parse=make_parse_profile(".md"),
        chunk=make_chunk_profile(),
    )
    async with ingestion_harness(postgres_database_url) as harness:
        document = await harness.upload(source, profile)
        await harness.run_next_task()
        [before] = await harness.segments(document.id)
        assert before.source_spans

        edited = "# 新手册\n\n运行 `actweave up`。"
        edited_index = build_index_text(edited)
        harness.fake_model.calls.clear()
        updated_view = await harness.module.update_segment(
            harness.resources.project_id,
            before.id,
            KnowledgeSegmentUpdate(content=edited),
            authority=harness.authority,
        )

        [updated] = await harness.segments(document.id)
        assert harness.fake_model.calls == [[edited_index]]
        assert updated.content == edited
        assert updated.index_text == edited_index
        assert updated.token_count == count_knowledge_tokens(edited_index)
        assert updated.source_spans == []
        assert updated.source_position == {"manual": True}
        assert updated.lexical_version == KNOWLEDGE_LEXICAL_VERSION
        assert updated_view.token_count == updated.token_count
        assert updated_view.source_spans == ()
        async with harness.resources.session_factory() as session:
            lexical_matches = await session.scalar(
                select(
                    KnowledgeSegmentRow.lexical_tsv
                    == func.to_tsvector(
                        "simple",
                        lexical_index_input(edited_index),
                    )
                ).where(KnowledgeSegmentRow.id == updated.id)
            )
        assert lexical_matches is True

        created_content = "## 补充\n\n字段：`List<int>`。"
        created_index = build_index_text(created_content)
        harness.fake_model.calls.clear()
        created = await harness.module.create_segment(
            harness.resources.project_id,
            document.id,
            KnowledgeSegmentCreate(content=created_content),
            authority=harness.authority,
        )
        assert harness.fake_model.calls == [[created_index]]
        created_row = await session_row(
            harness.resources.session_factory,
            created.id,
        )
        assert created_row.index_text == created_index
        assert created_row.token_count == count_knowledge_tokens(created_index)
        assert created_row.source_spans == []
        assert created_row.source_position == {"manual": True}
        assert created.token_count == created_row.token_count
        assert created.source_spans == ()

        listed, total = await harness.module.list_document_segments(
            harness.resources.project_id,
            document.id,
            authority=harness.authority,
        )
        assert total == 2
        assert [item.token_count for item in listed] == [
            updated.token_count,
            created_row.token_count,
        ]
        assert [item.source_spans for item in listed] == [(), ()]


async def session_row(session_factory, segment_id):  # noqa: ANN001, ANN202 - test helper
    async with session_factory() as session:
        row = await session.get(KnowledgeSegmentRow, segment_id)
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_manual_parent_child_update_uses_child_index_text_and_replaces_children(
    postgres_database_url: str,
    tmp_path,
) -> None:
    source = tmp_path / "parent.md"
    source.write_text("原始内容。" * 10, encoding="utf-8")
    profile = ProcessingProfile(
        parse=make_parse_profile(".md"),
        chunk=make_chunk_profile(
            mode="parent_child",
            size=200,
            overlap=0,
            child_size=100,
        ),
    )
    async with ingestion_harness(postgres_database_url) as harness:
        document = await harness.upload(source, profile)
        await harness.run_next_task()
        [parent] = await harness.segments(document.id)
        edited = "接口状态正常。" * 25
        harness.fake_model.calls.clear()

        await harness.module.update_segment(
            harness.resources.project_id,
            parent.id,
            KnowledgeSegmentUpdate(content=edited),
            authority=harness.authority,
        )

        async with harness.resources.session_factory() as session:
            children = list((await session.scalars(select(KnowledgeSegmentChildRow).where(KnowledgeSegmentChildRow.knowledge_segment_id == parent.id).order_by(KnowledgeSegmentChildRow.position))).all())
        assert len(children) == 2
        assert harness.fake_model.calls == [[child.index_text for child in children]]
        assert all(child.index_text == build_index_text(child.content) for child in children)
        assert all(child.token_count == count_knowledge_tokens(child.index_text) for child in children)
        assert all(child.source_spans == [] for child in children)
        [updated] = await harness.segments(document.id)
        assert updated.index_text == build_index_text(edited)
        assert updated.source_spans == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_old_profile_manual_mutations_leave_publication_unchanged(
    postgres_database_url: str,
    tmp_path,
    operation: str,
) -> None:
    """A stale frozen token profile leaves its published parent and children intact."""

    source = tmp_path / "parent.md"
    source.write_text("原始正文。" * 10, encoding="utf-8")
    profile = ProcessingProfile(
        parse=make_parse_profile(".md"),
        chunk=make_chunk_profile(mode="parent_child", size=200, child_size=100, overlap=0),
    )
    async with ingestion_harness(postgres_database_url) as harness:
        document = await harness.upload(source, profile)
        await harness.run_next_task()
        [parent] = await harness.segments(document.id)
        before = (parent.id, parent.content, parent.index_text, parent.document_version, parent.source_spans)
        async with harness.resources.session_factory() as session, session.begin():
            row = await session.get(KnowledgeDocumentRow, document.id)
            assert row is not None
            frozen = {
                **row.parsing_profile,
                "chunk": {**row.parsing_profile["chunk"], "splitter_version": "splitter-v2"},
            }
            row.parsing_profile = frozen
            before_children = list(
                (
                    await session.execute(
                        select(
                            KnowledgeSegmentChildRow.id,
                            KnowledgeSegmentChildRow.content,
                            KnowledgeSegmentChildRow.index_text,
                        )
                        .where(KnowledgeSegmentChildRow.knowledge_segment_id == parent.id)
                        .order_by(KnowledgeSegmentChildRow.position)
                    )
                ).all()
            )
        harness.fake_model.calls.clear()

        with pytest.raises(KnowledgeError) as error:
            if operation == "update":
                await harness.module.update_segment(
                    harness.resources.project_id,
                    parent.id,
                    KnowledgeSegmentUpdate(content="修改后的正文"),
                    authority=harness.authority,
                )
            else:
                await harness.module.create_segment(
                    harness.resources.project_id,
                    document.id,
                    KnowledgeSegmentCreate(content="新增正文"),
                    authority=harness.authority,
                )

        assert error.value.reason_code == "PROCESSING_PROFILE_UNAVAILABLE"
        assert harness.fake_model.calls == []
        [after] = await harness.segments(document.id)
        assert (after.id, after.content, after.index_text, after.document_version, after.source_spans) == before
        async with harness.resources.session_factory() as session:
            row = await session.get(KnowledgeDocumentRow, document.id)
            assert row is not None
            assert row.parsing_profile == frozen
            after_children = list(
                (
                    await session.execute(
                        select(
                            KnowledgeSegmentChildRow.id,
                            KnowledgeSegmentChildRow.content,
                            KnowledgeSegmentChildRow.index_text,
                        )
                        .where(KnowledgeSegmentChildRow.knowledge_segment_id == parent.id)
                        .order_by(KnowledgeSegmentChildRow.position)
                    )
                ).all()
            )
        assert after_children == before_children


async def _bindings(session_factory, segment_id):  # noqa: ANN001, ANN202
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(
                        KnowledgeSegmentAttachmentRow.position,
                        KnowledgeSegmentAttachmentRow.alt_text,
                        KnowledgeAttachmentRow.id,
                        KnowledgeAttachmentRow.sha256,
                    )
                    .join(
                        KnowledgeAttachmentRow,
                        KnowledgeAttachmentRow.id == KnowledgeSegmentAttachmentRow.attachment_id,
                    )
                    .where(KnowledgeSegmentAttachmentRow.segment_id == segment_id)
                    .order_by(KnowledgeSegmentAttachmentRow.position)
                )
            ).all()
        )


@pytest.mark.asyncio
async def test_manual_attachment_edits_rebuild_current_published_bindings(
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
        rows = await harness.segments(document.id)
        bound = [row for row in rows if await _bindings(harness.resources.session_factory, row.id)]
        assert len(bound) == 1
        segment = bound[0]
        [original] = await _bindings(harness.resources.session_factory, segment.id)
        match = re.search(
            r"!\[[^\]]*\]\(knowledge-attachment:([0-9a-f]{64})\)",
            segment.content,
        )
        assert match is not None and match.group(1) == original.sha256

        without_image = re.sub(
            r"\s*!\[[^\]]*\]\(knowledge-attachment:[0-9a-f]{64}\)\s*",
            "\n\n",
            segment.content,
        ).strip()
        await harness.module.update_segment(
            harness.resources.project_id,
            segment.id,
            KnowledgeSegmentUpdate(content=without_image),
            authority=harness.authority,
        )
        assert await _bindings(harness.resources.session_factory, segment.id) == []

        repeated = f"前置文字。\n\n![第一处](knowledge-attachment:{original.sha256})\n\n![第二处](knowledge-attachment:{original.sha256})"
        await harness.module.update_segment(
            harness.resources.project_id,
            segment.id,
            KnowledgeSegmentUpdate(content=repeated),
            authority=harness.authority,
        )
        rebound = await _bindings(harness.resources.session_factory, segment.id)
        assert [(row.position, row.alt_text, row.id) for row in rebound] == [
            (1, "第一处", original.id),
            (2, "第二处", original.id),
        ]

        created = await harness.module.create_segment(
            harness.resources.project_id,
            document.id,
            KnowledgeSegmentCreate(content=(f"补充说明。\n\n![补图](knowledge-attachment:{original.sha256})")),
            authority=harness.authority,
        )
        [created_binding] = await _bindings(
            harness.resources.session_factory,
            created.id,
        )
        assert (
            created_binding.position,
            created_binding.alt_text,
            created_binding.id,
        ) == (1, "补图", original.id)

        harness.fake_model.calls.clear()
        with pytest.raises(KnowledgeError) as error:
            await harness.module.update_segment(
                harness.resources.project_id,
                segment.id,
                KnowledgeSegmentUpdate(content=(f"非法引用。\n\n![越界](knowledge-attachment:{'f' * 64})")),
                authority=harness.authority,
            )
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert harness.fake_model.calls == []
        unchanged = await session_row(
            harness.resources.session_factory,
            segment.id,
        )
        assert unchanged.content == repeated
        assert (
            await _bindings(
                harness.resources.session_factory,
                segment.id,
            )
            == rebound
        )
