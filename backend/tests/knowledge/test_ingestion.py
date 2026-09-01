"""M4 gates: extraction, splitting, and the ingest pipeline.

Extractor and splitter tests are pure fixtures; pipeline tests run against the
installed Schema V1 snapshot with a fake object store, a fake model client, and
the production registry model port, so every database effect (processing flip,
publish transaction, late no-op) is exercised for real.
"""

from __future__ import annotations

import ast
import asyncio
import codecs
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
    PREVIEW_CHUNK_LIMIT,
    ExtractedBlock,
    KnowledgeIngestionHandler,
    clean_blocks,
    clean_text,
    decode_separator,
    extract_blocks,
    preview_document_chunks,
    split_blocks,
)
from actweave_knowledge.ingestion import pipeline as pipeline_module
from actweave_knowledge.ingestion import preview as preview_module
from actweave_knowledge.ingestion.splitter import (
    ChildDraft,
    SegmentDraft,
    attach_children,
    normalize_text,
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
from actweave_knowledge.tasks import deletion as deletion_module
from actweave_knowledge.tasks import worker as worker_module
from extraction_test_helpers import (
    ExtractionObjectStore,
    make_test_file_capability_provider,
    make_test_quota_port,
)
from ingestion_test_helpers import FakeModelClient as _FakeModelClient
from parsing_test_helpers import make_chunk_profile, make_parse_profile
from parsing_test_helpers import write_pdf as _write_pdf
from registry_helpers import registry_model_port, seed_embedding_model, seed_provider
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge.composition import is_knowledge_project_active
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.model_registry import ModelProviderModelRow

# ---------------------------------------------------------------------------
# Format fixtures
# ---------------------------------------------------------------------------


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    import docx

    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    document.save(str(path))


def _write_xlsx(path: Path, rows: list[list[object]]) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    for row in rows:
        sheet.append(row)
    workbook.save(str(path))


def _write_pptx(path: Path, slides: list[list[str]]) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    blank_layout = presentation.slide_layouts[6]
    for texts in slides:
        slide = presentation.slides.add_slide(blank_layout)
        for index, content in enumerate(texts):
            box = slide.shapes.add_textbox(Inches(1), Inches(1 + index), Inches(6), Inches(1))
            box.text_frame.text = content
    presentation.save(str(path))


def _write_epub(path: Path, chapters: list[tuple[str, str]]) -> None:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("k4-test-epub")
    book.set_title("测试书")
    book.set_language("zh")
    items = []
    for index, (title, body) in enumerate(chapters, start=1):
        chapter = epub.EpubHtml(title=title, file_name=f"ch{index}.xhtml", lang="zh")
        chapter.content = f"<html><body><h1>{title}</h1><p>{body}</p></body></html>"
        book.add_item(chapter)
        items.append(chapter)
    book.toc = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *items]
    epub.write_epub(str(path), book)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


def test_extract_pdf_pages_become_blocks_with_page_positions(tmp_path: Path) -> None:
    path = tmp_path / "guide.pdf"
    _write_pdf(path, ["Knowledge page one", "Knowledge page two"])

    blocks = extract_blocks(path, ".pdf")

    texts = [block.text.strip() for block in blocks]
    assert "Knowledge page one" in texts[0]
    assert "Knowledge page two" in texts[1]
    assert [block.source_position for block in blocks] == [{"page": 1}, {"page": 2}]


def test_extract_docx_paragraphs_become_blocks(tmp_path: Path) -> None:
    path = tmp_path / "guide.docx"
    _write_docx(path, ["第一段：产品概述。", "第二段：安装步骤。"])

    blocks = extract_blocks(path, ".docx")

    assert [block.text for block in blocks] == ["第一段：产品概述。", "第二段：安装步骤。"]
    assert blocks[0].source_position == {"paragraph": 1}
    assert blocks[1].source_position == {"paragraph": 2}


@pytest.mark.parametrize("with_surrounding_paragraphs", [False, True])
def test_extract_docx_includes_table_text_in_document_order(tmp_path: Path, with_surrounding_paragraphs: bool) -> None:
    import docx

    document = docx.Document()
    if with_surrounding_paragraphs:
        document.add_paragraph("服务器故障手册")
    table = document.add_table(rows=2, cols=2)
    for cell, value in zip(
        (cell for row in table.rows for cell in row.cells),
        ("故障代码", "处置步骤", "E42", "重启网关服务"),
        strict=True,
    ):
        cell.text = value
    if with_surrounding_paragraphs:
        document.add_paragraph("操作完成后确认服务状态")
    path = tmp_path / "table.docx"
    document.save(str(path))

    blocks = extract_blocks(path, ".docx")

    expected = ["故障代码\t处置步骤", "E42\t重启网关服务"]
    if with_surrounding_paragraphs:
        expected = ["服务器故障手册", *expected, "操作完成后确认服务状态"]
    assert [block.text for block in blocks] == expected
    assert [block.source_position for block in blocks if "table" in block.source_position] == [{"table": 1, "row": 1}, {"table": 1, "row": 2}]


def test_extract_docx_merged_table_cells_are_counted_once(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "故障指南"
    table.cell(1, 0).text = "E42"
    table.cell(1, 1).text = "重启网关服务"
    path = tmp_path / "merged.docx"
    document.save(str(path))

    blocks = extract_blocks(path, ".docx", max_total_chars=14)

    assert [block.text for block in blocks] == ["故障指南", "E42\t重启网关服务"]
    with pytest.raises(KnowledgeError) as error:
        extract_blocks(path, ".docx", max_total_chars=13)
    assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED


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


def test_extract_csv_rows_join_cells_and_skip_empty_rows(tmp_path: Path) -> None:
    path = tmp_path / "table.csv"
    path.write_text('名称,数量\n"苹果, 红",3\n,,\n梨,5\n', encoding="utf-8")

    blocks = extract_blocks(path, ".csv")

    assert [block.text for block in blocks] == ["名称, 数量", "苹果, 红, 3", "梨, 5"]
    assert [block.source_position["row"] for block in blocks] == [1, 2, 4]


def test_extract_xlsx_rows_carry_sheet_and_row_positions(tmp_path: Path) -> None:
    path = tmp_path / "table.xlsx"
    _write_xlsx(path, [["名称", "数量"], [None, None], ["苹果", 3]])

    blocks = extract_blocks(path, ".xlsx")

    assert [block.text for block in blocks] == ["名称, 数量", "苹果, 3"]
    assert blocks[0].source_position == {"sheet": "数据", "row": 1}
    assert blocks[1].source_position == {"sheet": "数据", "row": 3}


@pytest.mark.parametrize("extension", [".html", ".htm"])
def test_extract_html_keeps_visible_text_and_drops_script_style(tmp_path: Path, extension: str) -> None:
    path = tmp_path / f"page{extension}"
    path.write_bytes(
        ("<html><head><meta charset='utf-8'><style>body{color:red}</style><script>alert('忽略我')</script></head><body><h1>产品指南</h1><p>第一段说明。</p><template>模板内容</template><p>第二段说明。</p></body></html>").encode()
    )

    blocks = extract_blocks(path, extension)

    assert len(blocks) == 1
    assert blocks[0].text == "产品指南\n第一段说明。\n第二段说明。"
    assert blocks[0].source_position == {}


def test_extract_html_rejects_markup_without_visible_text(tmp_path: Path) -> None:
    path = tmp_path / "empty.html"
    path.write_text("<html><body><script>var x = 1;</script></body></html>", encoding="utf-8")

    with pytest.raises(KnowledgeError) as error:
        extract_blocks(path, ".html")
    assert error.value.code == KNOWLEDGE_PARSE_FAILED


def test_extract_pptx_slides_become_blocks_with_slide_positions(tmp_path: Path) -> None:
    path = tmp_path / "deck.pptx"
    _write_pptx(path, [["发布计划", "里程碑一"], ["回滚方案"]])

    blocks = extract_blocks(path, ".pptx")

    assert [block.text for block in blocks] == ["发布计划\n里程碑一", "回滚方案"]
    assert [block.source_position for block in blocks] == [{"slide": 1}, {"slide": 2}]


def test_extract_epub_chapters_become_blocks_and_skip_navigation(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    _write_epub(path, [("第一章", "开篇内容。"), ("第二章", "后续内容。")])

    blocks = extract_blocks(path, ".epub")

    assert [block.source_position for block in blocks] == [{"chapter": 1}, {"chapter": 2}]
    assert blocks[0].text == "第一章\n开篇内容。"
    assert blocks[1].text == "第二章\n后续内容。"


@pytest.mark.parametrize(
    ("payload", "extension"),
    [
        pytest.param("知识库测试文本。".encode(), ".txt", id="utf8"),
        pytest.param(codecs.BOM_UTF16_LE + "知识库测试文本。".encode("utf-16-le"), ".txt", id="utf16-le-bom"),
        pytest.param(codecs.BOM_UTF16_BE + "知识库测试文本。".encode("utf-16-be"), ".md", id="utf16-be-bom"),
        pytest.param("知识库测试文本。".encode("gb18030"), ".txt", id="gb18030"),
    ],
)
def test_extract_text_decodes_supported_encodings(tmp_path: Path, payload: bytes, extension: str) -> None:
    path = tmp_path / f"note{extension}"
    path.write_bytes(payload)

    blocks = extract_blocks(path, extension)

    assert blocks == [ExtractedBlock(text="知识库测试文本。", source_position={})]


@pytest.mark.parametrize(
    ("payload", "extension", "reason"),
    [
        pytest.param(b"", ".txt", "empty text file", id="empty-txt"),
        pytest.param(b"   \n\n  ", ".md", "whitespace only", id="whitespace-md"),
        pytest.param(b"\x81", ".txt", "undecodable bytes", id="undecodable"),
        pytest.param(b"not a real pdf", ".pdf", "corrupt pdf", id="corrupt-pdf"),
        pytest.param(b"PK\x03\x04broken", ".docx", "corrupt docx", id="corrupt-docx"),
        pytest.param(b"PK\x03\x04broken", ".xlsx", "corrupt xlsx", id="corrupt-xlsx"),
        pytest.param(b"PK\x03\x04broken", ".pptx", "corrupt pptx", id="corrupt-pptx"),
        pytest.param(b"PK\x03\x04broken", ".epub", "corrupt epub", id="corrupt-epub"),
    ],
)
def test_extract_rejects_empty_and_corrupt_files(tmp_path: Path, payload: bytes, extension: str, reason: str) -> None:
    path = tmp_path / f"bad{extension}"
    path.write_bytes(payload)

    with pytest.raises(KnowledgeError) as error:
        extract_blocks(path, extension)
    assert error.value.code == KNOWLEDGE_PARSE_FAILED, reason


def test_extract_rejects_unknown_extensions(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG")

    with pytest.raises(KnowledgeError) as error:
        extract_blocks(path, ".png")
    assert error.value.code == KNOWLEDGE_PARSE_FAILED


def test_extract_aborts_as_soon_as_text_exceeds_the_char_budget(tmp_path: Path) -> None:
    """The cumulative budget stops accumulation mid-file, not after the fact."""

    path = tmp_path / "big.csv"
    path.write_text("\n".join(f"行{index},{'字' * 40}" for index in range(100)), encoding="utf-8")

    with pytest.raises(KnowledgeError) as error:
        extract_blocks(path, ".csv", max_total_chars=200)
    assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED

    # The same file is fine without a budget.
    assert extract_blocks(path, ".csv")


def test_extract_pdf_budget_stops_page_accumulation(tmp_path: Path) -> None:
    path = tmp_path / "long.pdf"
    _write_pdf(path, ["page one text " * 10, "page two text " * 10])

    with pytest.raises(KnowledgeError) as error:
        extract_blocks(path, ".pdf", max_total_chars=100)
    assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED


@pytest.mark.parametrize("extension", [".docx", ".xlsx", ".pptx", ".epub"])
def test_extract_rejects_zip_containers_with_bomb_sized_declared_contents(tmp_path: Path, extension: str) -> None:
    """Zip-container decompression bombs are refused before any XML is parsed."""

    import zipfile

    path = tmp_path / f"bomb{extension}"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\0" * (65 * 1024 * 1024))
    assert path.stat().st_size < 1024 * 1024  # the attack: tiny upload, huge content

    with pytest.raises(KnowledgeError) as error:
        extract_blocks(path, extension, max_total_chars=1000)
    assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED


# ---------------------------------------------------------------------------
# Cleaner and splitter
# ---------------------------------------------------------------------------


def test_normalize_text_unifies_newlines_and_compresses_blanks() -> None:
    raw = "第一行  \r\n\r\n\r\n\r第二行\t\n  缩进保留\r\n"

    assert normalize_text(raw) == "第一行\n\n第二行\n  缩进保留"


def test_split_keeps_short_text_as_one_chunk_with_default_parameters() -> None:
    blocks = [ExtractedBlock(text="短文本。", source_position={"page": 1})]

    drafts = split_blocks(blocks, chunk_size=1000, chunk_overlap=100)

    assert len(drafts) == 1
    assert drafts[0].position == 1
    assert drafts[0].content == "短文本。"
    assert drafts[0].source_position == {"page": 1}


def test_split_prefers_paragraph_boundaries() -> None:
    first = "A" * 500
    second = "B" * 700
    blocks = [ExtractedBlock(text=f"{first}\n\n{second}")]

    drafts = split_blocks(blocks, chunk_size=1000, chunk_overlap=100)

    assert [draft.position for draft in drafts] == [1, 2]
    assert drafts[0].content == first
    assert drafts[1].content == second


def test_split_packs_small_paragraphs_up_to_chunk_size() -> None:
    paragraphs = [f"段落{index}内容" for index in range(1, 5)]
    blocks = [ExtractedBlock(text="\n\n".join(paragraphs))]

    drafts = split_blocks(blocks, chunk_size=1000, chunk_overlap=100)

    assert len(drafts) == 1
    assert drafts[0].content == "\n\n".join(paragraphs)


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


def test_split_zero_overlap_covers_text_without_duplication() -> None:
    text_value = "D" * 1000
    drafts = split_blocks([ExtractedBlock(text=text_value)], chunk_size=200, chunk_overlap=0)

    assert "".join(draft.content for draft in drafts) == text_value


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


def test_split_positions_stay_contiguous_across_blocks() -> None:
    blocks = [
        ExtractedBlock(text="E" * 500, source_position={"page": 1}),
        ExtractedBlock(text="", source_position={"page": 2}),  # skipped
        ExtractedBlock(text="F" * 500, source_position={"page": 3}),
    ]

    drafts = split_blocks(blocks, chunk_size=300, chunk_overlap=0)

    assert [draft.position for draft in drafts] == list(range(1, len(drafts) + 1))
    assert {draft.source_position["page"] for draft in drafts} == {1, 3}


def test_split_respects_minimum_and_maximum_chunk_bounds() -> None:
    text_value = "G" * 9000
    smallest = split_blocks([ExtractedBlock(text=text_value)], chunk_size=200, chunk_overlap=0)
    largest = split_blocks([ExtractedBlock(text=text_value)], chunk_size=4000, chunk_overlap=500)

    assert all(len(draft.content) <= 200 for draft in smallest)
    assert all(len(draft.content) <= 4000 for draft in largest)
    assert len(smallest) > len(largest)


def test_decode_separator_handles_escapes_and_leaves_other_text_verbatim() -> None:
    assert decode_separator("\\n\\n") == "\n\n"
    assert decode_separator("\\t") == "\t"
    assert decode_separator("\\r\\n") == "\r\n"
    assert decode_separator("。") == "。"
    assert decode_separator("###") == "###"
    # Unknown escapes stay literal instead of being mangled.
    assert decode_separator("\\x41") == "\\x41"


def test_split_honors_a_custom_separator_before_the_fallback_sequence() -> None:
    text_value = "第一节####第二节####第三节"
    drafts = split_blocks([ExtractedBlock(text=text_value)], chunk_size=200, chunk_overlap=0, separator="####")

    assert len(drafts) == 1  # small pieces pack back into one chunk
    assert drafts[0].content == text_value

    tight = split_blocks([ExtractedBlock(text=text_value)], chunk_size=8, chunk_overlap=0, separator="####")
    assert [draft.content for draft in tight] == ["第一节####", "第二节####", "第三节"]


def test_split_chinese_sentences_fall_back_to_the_full_stop_boundary() -> None:
    sentences = ["第一句话内容比较长一些。", "第二句话也有不少内容。", "第三句话继续增加长度。"]
    drafts = split_blocks([ExtractedBlock(text="".join(sentences))], chunk_size=20, chunk_overlap=0)

    assert [draft.content for draft in drafts] == [
        sentences[0],
        sentences[1],
        sentences[2],
    ]


def test_split_custom_escaped_separator_matches_literal_newline() -> None:
    text_value = "甲部分\n乙部分\n丙部分"
    drafts = split_blocks([ExtractedBlock(text=text_value)], chunk_size=5, chunk_overlap=0, separator="\\n")

    assert [draft.content for draft in drafts] == ["甲部分", "乙部分", "丙部分"]


def test_split_child_chunks_merges_pieces_and_hard_cuts_oversized_ones() -> None:
    # Pieces pack up to the child size without overlap; an oversized piece
    # falls through to the fallback boundaries and finally hard cuts.
    text_value = "短句一。短句二。" + "长" * 25 + "。尾句。"
    children = split_child_chunks(text_value, child_chunk_size=10, child_chunk_separator="。")

    assert children[0] == "短句一。短句二。"
    assert all(len(child) <= 10 for child in children)
    assert "".join(children).replace("。", "") == text_value.replace("。", "")


def test_split_child_chunks_decodes_escaped_separators() -> None:
    children = split_child_chunks("第一行\n第二行\n第三行", child_chunk_size=4, child_chunk_separator="\\n")

    assert children == ("第一行", "第二行", "第三行")


def test_attach_children_populates_every_draft_in_order() -> None:
    drafts = [
        SegmentDraft(position=1, content="甲一。甲二。", source_position={"page": 1}),
        SegmentDraft(position=2, content="乙一。", source_position={"page": 2}),
    ]

    attached = attach_children(drafts, child_chunk_size=100, child_chunk_separator="。")

    assert all(isinstance(child, ChildDraft) for draft in attached for child in draft.children)
    assert [tuple(child.content for child in draft.children) for draft in attached] == [("甲一。甲二。",), ("乙一。",)]
    # Original identity fields survive untouched.
    assert [(draft.position, draft.source_position) for draft in attached] == [(1, {"page": 1}), (2, {"page": 2})]


# ---------------------------------------------------------------------------
# Cleaner
# ---------------------------------------------------------------------------


def test_clean_text_with_both_rules_off_changes_nothing() -> None:
    raw = "hello   world https://example.com a@b.co\n\n\nnext"
    assert clean_text(raw, remove_extra_spaces=False, remove_urls_emails=False) == raw


def test_clean_text_remove_extra_spaces_compresses_whitespace_and_newlines() -> None:
    raw = "甲  乙\u3000\u3000丙\tX\n\n\n\n下一段"
    cleaned = clean_text(raw, remove_extra_spaces=True, remove_urls_emails=False)

    assert cleaned == "甲 乙 丙\tX\n\n下一段"


def test_clean_text_remove_urls_emails_strips_links_and_addresses() -> None:
    raw = "联系 someone.name+tag@example-domain.co.uk 或访问 https://example.com/path?q=1 了解 http://foo.bar 详情"
    cleaned = clean_text(raw, remove_extra_spaces=False, remove_urls_emails=True)

    assert "example.com" not in cleaned
    assert "@" not in cleaned
    assert "foo.bar" not in cleaned
    assert cleaned.startswith("联系 ")
    assert "了解" in cleaned and "详情" in cleaned


def test_clean_text_url_removal_stops_at_cjk_text() -> None:
    # Chinese prose rarely puts whitespace after a URL; the characters that
    # follow belong to the document and must survive.
    raw = "详见https://x.invalid/page下一节，以及http://y.invalid/z。结束"
    cleaned = clean_text(raw, remove_extra_spaces=False, remove_urls_emails=True)

    assert cleaned == "详见下一节，以及。结束"


def test_clean_blocks_normalizes_before_rules_and_keeps_positions() -> None:
    blocks = [
        ExtractedBlock(text="第一行  x\r\n\r\n\r\n\r\n第二行", source_position={"page": 2}),
        ExtractedBlock(text="纯文本", source_position={"page": 3}),
    ]

    cleaned = clean_blocks(blocks, remove_extra_spaces=True, remove_urls_emails=False)

    # CRLF newlines normalize to \n first, so the 3+-newline rule applies.
    assert cleaned[0].text == "第一行 x\n\n第二行"
    assert cleaned[0].source_position == {"page": 2}
    assert cleaned[1].text == "纯文本"

    untouched = clean_blocks(blocks, remove_extra_spaces=False, remove_urls_emails=False)
    assert untouched is blocks


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
async def test_preview_returns_first_chunks_and_total(tmp_path: Path) -> None:
    paragraphs = [f"第{index}段" + "内容" * 180 for index in range(1, 15)]
    source = tmp_path / "preview.md"
    source.write_text("\n\n".join(paragraphs), encoding="utf-8")

    settings = KnowledgeSettings.model_validate({"enabled": False})
    preview = await _preview_chunks(_preview_request(source, chunk_size=250, chunk_overlap=0), settings)

    assert preview.total == 14
    assert len(preview.chunks) == PREVIEW_CHUNK_LIMIT
    assert [chunk.position for chunk in preview.chunks] == list(range(1, PREVIEW_CHUNK_LIMIT + 1))
    assert preview.chunks[0].content == paragraphs[0]
    assert preview.chunks[0].word_count == len(paragraphs[0])


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


@pytest.mark.asyncio
async def test_preview_of_empty_document_surfaces_parse_failed(tmp_path: Path) -> None:
    source = tmp_path / "blank.txt"
    source.write_text("   \n\n   ", encoding="utf-8")
    settings = KnowledgeSettings.model_validate({"enabled": False})

    with pytest.raises(KnowledgeError) as error:
        await _preview_chunks(_preview_request(source), settings)
    assert error.value.code == KNOWLEDGE_PARSE_FAILED


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
    await _install_full_schema(engine)
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


# ---------------------------------------------------------------------------
# Blocking-I/O static gate
# ---------------------------------------------------------------------------

# Direct calls that would block the event loop inside an async function.
# asyncio.to_thread passes callables as arguments (not Call nodes), so wrapped
# usage never triggers a violation.
_BLOCKING_CALLS = frozenset(
    {
        "extract_blocks",
        "split_blocks",
        "mkdtemp",
        "rmtree",
        "open",
        "read_bytes",
        "write_bytes",
        "PdfReader",
        "load_workbook",
        "fput_object",
        "fget_object",
        "remove_object",
        "bucket_exists",
    }
)


def _direct_calls_in_async_functions(source_path: Path, blocked: frozenset[str]) -> list[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        stack: list[ast.AST] = list(node.body)
        while stack:
            current = stack.pop()
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # nested defs are separate execution contexts
            if isinstance(current, ast.Call):
                target = current.func
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if name in blocked:
                    violations.append(f"{source_path.name}:{current.lineno} {node.name} calls {name}() directly")
            stack.extend(ast.iter_child_nodes(current))
    return violations


@pytest.mark.parametrize(
    "module",
    [
        pytest.param(pipeline_module, id="ingestion-pipeline"),
        pytest.param(preview_module, id="chunk-preview"),
        pytest.param(worker_module, id="task-worker"),
        pytest.param(deletion_module, id="deletion-handlers"),
    ],
)
def test_m4_async_code_never_blocks_the_event_loop(module) -> None:  # noqa: ANN001
    module_path = Path(module.__file__)
    assert _direct_calls_in_async_functions(module_path, _BLOCKING_CALLS) == []
