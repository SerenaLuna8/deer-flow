"""Plain source text must not acquire Markdown meaning during extraction."""

import pytest
from actweave_knowledge.extraction.literal import escape_literal_text
from actweave_knowledge.ingestion.index_text import build_index_text, has_indexable_source_text
from markdown_it import MarkdownIt
from parsing_test_helpers import make_document

LITERALS = (
    "# text",
    "- item",
    "+ item",
    "1. item",
    "1) item",
    "> text",
    "---",
    "text\n===",
    "```",
    "a|b",
    "[x](https://example.invalid)",
    "![x](https://example.invalid)",
    r"\path",
    "&amp;",
    "&#35;",
    "    # indent",
    "\t# indent",
    " \t# indent",
)


@pytest.mark.parametrize("raw", LITERALS)
def test_literal_serializer_preserves_visible_text_without_structure(raw):
    rendered = escape_literal_text(raw)
    assert build_index_text(rendered) == raw.strip()
    assert has_indexable_source_text((make_document(rendered),))
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = parser.parse(rendered)
    prohibited = {
        "heading_open",
        "bullet_list_open",
        "ordered_list_open",
        "blockquote_open",
        "table_open",
        "hr",
        "fence",
        "code_block",
    }
    assert not any(token.type in prohibited for token in tokens)
    assert not any(child.type in {"image", "link_open"} for token in tokens for child in token.children or ())


@pytest.mark.parametrize(
    "pieces",
    [("1", ". item"), ("1", "2. item"), ("1", ") item"), ("text\n", "=", "=="), ("-", " item")],
)
def test_fragmented_markers_need_no_mutable_serializer_state(pieces):
    rendered = "".join(escape_literal_text(piece, protect_indentation=False) for piece in pieces)
    assert build_index_text(rendered) == "".join(pieces)


def test_serializer_keeps_ordinary_punctuation_and_supplies_context_options():
    ordinary = "Knowledge parser readiness. SGVsbG8= a-b 10.0.0.1"
    assert escape_literal_text(ordinary) == ordinary
    assert escape_literal_text("a|b", escape_pipes=False) == "a|b"
    assert escape_literal_text("    x", protect_indentation=False) == "    x"
    assert escape_literal_text("    x").startswith("&#32;   x")
    assert escape_literal_text("\tx").startswith("&#9;x")


def test_long_literal_text_survives_parent_child_token_budgets():
    from actweave_knowledge.ingestion.splitter import split_documents
    from actweave_knowledge.ingestion.tokenizer import count_knowledge_tokens
    from parsing_test_helpers import make_chunk_profile

    raw = " ".join(f"item{number} &amp; [x](url) # - | \\path" for number in range(120))
    document = make_document(escape_literal_text(raw))
    profile = make_chunk_profile(mode="parent_child", size=200, overlap=0, child_size=100)
    drafts = split_documents((document,), profile=profile)
    assert len(drafts) > 1
    combined = " ".join(draft.index_text for draft in drafts)
    child_content = " ".join(child.content for draft in drafts for child in draft.children)
    child_index = " ".join(child.index_text for draft in drafts for child in draft.children)
    assert child_content == " ".join(draft.content for draft in drafts)
    assert child_index == combined
    assert child_index == " ".join(build_index_text(child.content) for draft in drafts for child in draft.children)
    for number in range(120):
        assert combined.count(f"item{number} ") == 1
        assert child_index.count(f"item{number} ") == 1
    assert combined.count("&amp;") == 120
    assert combined.count("[x](url)") == 120
    assert combined.count(r"\path") == 120
    assert child_index.count("&amp;") == 120
    assert child_index.count("[x](url)") == 120
    assert child_index.count(r"\path") == 120
    for draft in drafts:
        assert draft.children
        for content, indexed, budget in (
            (draft.content, draft.index_text, profile.size),
            *((child.content, child.index_text, profile.child_size) for child in draft.children),
        ):
            assert len(content) <= 16000
            assert count_knowledge_tokens(content) <= budget
            assert count_knowledge_tokens(indexed) <= budget
            assert build_index_text(content) == indexed


@pytest.mark.parametrize("raw", LITERALS)
def test_txt_serializes_each_source_line_before_assigning_offsets(tmp_path, raw):
    from actweave_knowledge.extraction.processor import ExtractProcessor
    from parsing_test_helpers import make_context, make_setting

    path = tmp_path / "literal.txt"
    path.write_text(raw, encoding="utf-8")
    (document,) = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert build_index_text(document.page_content) == raw.strip()
    assert has_indexable_source_text((document,))
    assert "".join(document.page_content[span.start : span.end] for span in document.source_spans) == document.page_content
    assert [span.location["line"] for span in document.source_spans] == list(range(1, len(raw.splitlines()) + 1))
    assert all(span.role == "source" for span in document.source_spans)
    assert not document.attachments and not document.warnings


@pytest.mark.parametrize("raw", LITERALS[:15])
def test_html_text_leaves_preserve_literal_meaning(raw):
    from html import escape

    from actweave_knowledge.extraction.builtin.html_extractor import html_to_documents

    markup = "<p>" + escape(raw).replace("\n", "<br>") + "</p>"
    (document,) = html_to_documents(markup)
    assert build_index_text(document.page_content) == raw
    assert has_indexable_source_text((document,))
    assert "".join(document.page_content[s.start : s.end] for s in document.source_spans) == document.page_content


@pytest.mark.parametrize("category", ["NarrativeText", "Title", "Table"])
@pytest.mark.parametrize("raw", ["---", "1. item", "&amp;", "    # indent"])
def test_unstructured_plain_elements_and_fallback_preserve_text(raw, category):
    from types import SimpleNamespace

    from actweave_knowledge.extraction.unstructured_local.elements import elements_to_documents

    metadata = SimpleNamespace(page_number=3, category_depth=1, text_as_html=None)
    element = SimpleNamespace(text=raw, category=category, metadata=metadata)
    (document,) = elements_to_documents([element], kind="slide")
    assert build_index_text(document.page_content) == raw.strip()
    assert has_indexable_source_text((document,))
    assert document.source_spans[0].location == {"element": 1, "slide": 3}
    assert document.source_spans[0].end == len(document.page_content)
    assert document.heading_path == ((raw,) if category == "Title" else ())
    assert {warning.code for warning in document.warnings} == ({"TABLE_STRUCTURE_UNAVAILABLE"} if category == "Table" else set())


def test_html_structure_and_code_bypass_literal_serializer():
    from actweave_knowledge.extraction.builtin.html_extractor import html_to_documents

    markup = (
        "<h2>Heading</h2><p>1<span>. item</span></p>"
        '<ol start="3"><li>listed</li></ol>'
        "<blockquote><p>quoted</p></blockquote>"
        '<p><a href="https://example.invalid/docs">label &amp;amp;</a></p>'
        "<pre>---\n&amp;amp;\n![x](url)</pre>"
        "<p><code>&amp;amp; | [x](url)</code></p>"
    )
    documents = html_to_documents(markup)
    markdown = "\n\n".join(doc.page_content for doc in documents)
    tokens = MarkdownIt("commonmark", {"html": False}).parse(markdown)
    assert sum(token.type == "heading_open" for token in tokens) == 1
    assert sum(token.type == "ordered_list_open" for token in tokens) == 1
    assert sum(token.type == "blockquote_open" for token in tokens) == 1
    assert [token.content for token in tokens if token.type == "fence"] == ["---\n&amp;\n![x](url)\n"]
    assert "1. item" in build_index_text(markdown)
    assert "label &amp;" in build_index_text(markdown)
    assert "&amp; | [x](url)" in build_index_text(markdown)
    assert "https://example.invalid/docs" in markdown
    assert "https://example.invalid/docs" not in build_index_text(markdown)


def test_html_table_escapes_leaf_pipes_once_and_leaves_code_text_intact():
    from actweave_knowledge.extraction.builtin.html_extractor import html_to_documents

    (document,) = html_to_documents("<table><tr><th>key</th><th>value</th></tr><tr><td><p>a|b &amp;amp;</p></td><td><code>a\\|b</code></td></tr><tr><td>c\\|d</td><td><strong>x|y</strong></td></tr></table>")
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(document.page_content)
    assert sum(token.type == "table_open" for token in tokens) == 1
    assert sum(token.type == "td_open" for token in tokens) == 4
    assert build_index_text(document.page_content).splitlines() == [
        "key",
        "value",
        "a|b &amp;",
        "a\\|b",
        "c\\|d",
        "x|y",
    ]
    assert "".join(document.page_content[s.start : s.end] for s in document.source_spans) == document.page_content


def test_html_heading_with_safe_link_and_literals_keeps_source_backed_context():
    from actweave_knowledge.extraction.builtin.html_extractor import html_to_documents
    from actweave_knowledge.ingestion.structure import structure_groups

    heading = 'Intro <a href="https://example.invalid/docs">docs</a> .*literal* &amp;amp;'
    documents = html_to_documents(f"<h1>{heading}</h1><p>{'body ' * 500}</p>")
    assert documents[0].heading_path == ("Intro docs .*literal* &amp;",)
    assert "[docs](<https://example.invalid/docs>)" in documents[0].page_content
    prefix, pieces, _ = next(group for group in structure_groups(tuple(documents)) if any("body" in piece.content for piece in group[1]))
    assert "# Intro [docs](<https://example.invalid/docs>) \\.\\*literal\\* \\&amp;" == prefix.content
    assert build_index_text(prefix.content) == "Intro docs .*literal* &amp;"
    assert prefix.source_spans and all(span.role == "source" for span in prefix.source_spans)
    assert pieces and all(piece.source_spans for piece in pieces)


def test_unstructured_literal_title_reuses_source_backed_context_when_body_splits():
    from actweave_knowledge.extraction.unstructured_local.elements import elements_to_documents
    from actweave_knowledge.ingestion.splitter import split_documents
    from actweave_knowledge.ingestion.structure import structure_groups
    from actweave_knowledge.ingestion.tokenizer import count_knowledge_tokens
    from parsing_test_helpers import make_chunk_profile
    from unstructured.documents.elements import ElementMetadata, NarrativeText, Title

    raw = "Intro &amp; [literal](url)"
    documents = elements_to_documents([Title(raw, metadata=ElementMetadata(page_number=1)), NarrativeText("body " * 700, metadata=ElementMetadata(page_number=1))], kind="slide")
    assert all(document.heading_path == (raw,) for document in documents)
    title = documents[0]
    assert build_index_text(title.page_content) == raw
    assert not any(token.type == "heading_open" for token in MarkdownIt("commonmark").parse(title.page_content))
    prefix, _, _ = next(group for group in structure_groups(tuple(documents)) if any("body" in part.content for part in group[1]))
    assert prefix.content == "# " + title.page_content
    assert "".join(prefix.content[s.start : s.end] for s in prefix.source_spans) == title.page_content
    assert all(s.role == "source" for s in prefix.source_spans)
    drafts = split_documents(tuple(documents), profile=make_chunk_profile(mode="parent_child", size=200, overlap=0, child_size=100))
    assert len(drafts) > 2
    for values, limit in ((drafts, 200), ([child for draft in drafts for child in draft.children], 100)):
        assert sum(value.index_text.count("body") for value in values) == 700
        for index, value in enumerate(values):
            assert value.index_text.startswith(raw)
            assert count_knowledge_tokens(value.content) <= limit
            assert count_knowledge_tokens(value.index_text) <= limit
            assert len(value.content) <= 16000
            spans = [s for s in value.source_spans if s.block_id == title.source_spans[0].block_id]
            assert spans
            assert all(s.role == ("source" if index == 0 else "context_prefix") for s in spans)
            assert all(s.location == {"element": 1, "slide": 1} for s in spans)
            assert "".join(value.content[s.start : s.end] for s in spans) == title.page_content


@pytest.mark.parametrize("raw", ["  Intro &amp; [literal](url)  ", "Intro &amp;\n[literal](url)"], ids=["edge-whitespace", "multiline"])
def test_unstructured_title_identity_keeps_whitespace_and_multiline_context(raw):
    from actweave_knowledge.extraction.unstructured_local.elements import elements_to_documents
    from actweave_knowledge.ingestion.splitter import split_documents
    from actweave_knowledge.ingestion.structure import structure_groups
    from actweave_knowledge.ingestion.tokenizer import count_knowledge_tokens
    from parsing_test_helpers import make_chunk_profile
    from unstructured.documents.elements import ElementMetadata, NarrativeText, Title

    documents = elements_to_documents([Title(raw, metadata=ElementMetadata(page_number=1)), NarrativeText("body " * 700, metadata=ElementMetadata(page_number=1))], kind="slide")
    assert all(document.heading_path == (raw,) for document in documents)
    title = documents[0]
    assert title.kind == "title"
    assert build_index_text(title.page_content) == raw.strip()
    prefix, _, _ = next(group for group in structure_groups(tuple(documents)) if any("body" in part.content for part in group[1]))
    assert prefix.content == "# " + title.page_content
    assert "".join(prefix.content[s.start : s.end] for s in prefix.source_spans) == title.page_content
    assert all(s.role == "source" for s in prefix.source_spans)

    drafts = split_documents(tuple(documents), profile=make_chunk_profile(size=200, overlap=0, child_size=100))
    assert len(drafts) > 2
    assert sum(draft.index_text.count("body") for draft in drafts) == 700
    for index, draft in enumerate(drafts):
        assert draft.content.startswith("# " + title.page_content)
        assert draft.index_text.startswith(raw.strip() + "\nbody")
        assert count_knowledge_tokens(draft.content) <= 200
        assert count_knowledge_tokens(draft.index_text) <= 200
        assert len(draft.content) <= 16000
        spans = [s for s in draft.source_spans if s.block_id == title.source_spans[0].block_id]
        assert spans
        assert all(s.role == ("source" if index == 0 else "context_prefix") for s in spans)
        assert all(s.location == {"element": 1, "slide": 1} for s in spans)
        assert "".join(draft.content[s.start : s.end] for s in spans) == title.page_content
    assert all(document.heading_path == (raw,) for document in documents)


@pytest.mark.parametrize("indent", ["    ", "\t"], ids=["four-spaces", "tab"])
def test_unstructured_midline_indent_stays_literal_in_parent_and_child(indent):
    from actweave_knowledge.extraction.unstructured_local.elements import elements_to_documents
    from actweave_knowledge.ingestion.splitter import split_documents
    from actweave_knowledge.ingestion.tokenizer import count_knowledge_tokens
    from parsing_test_helpers import make_chunk_profile
    from unstructured.documents.elements import ElementMetadata, NarrativeText, Title

    raw = "alpha " * 194 + "boundary" + indent + "# literal &amp; " + "omega " * 250
    documents = elements_to_documents([Title("Context", metadata=ElementMetadata(page_number=1)), NarrativeText(raw, metadata=ElementMetadata(page_number=1))], kind="slide")
    assert all(document.heading_path == ("Context",) for document in documents)
    assert "&#32;" not in documents[1].page_content and "&#9;" not in documents[1].page_content
    drafts = split_documents(tuple(documents), profile=make_chunk_profile(mode="parent_child", size=200, overlap=0, child_size=100, separator="boundary", child_separator="boundary"))
    assert len(drafts) > 1 and all(draft.children for draft in drafts)
    for values, limit in ((drafts, 200), ([child for draft in drafts for child in draft.children], 100)):
        indexed = " ".join(value.index_text.removeprefix("Context\n") for value in values)
        assert " ".join(indexed.split()) == " ".join(raw.split())
        source = " ".join("".join(value.content[s.start : s.end] for s in value.source_spans if s.block_id == documents[1].source_spans[0].block_id) for value in values)
        assert " ".join(build_index_text(source).split()) == " ".join(raw.split())
        for index, value in enumerate(values):
            assert value.content.startswith("# Context\n\n")
            assert count_knowledge_tokens(value.content) <= limit
            assert count_knowledge_tokens(value.index_text) <= limit
            assert len(value.content) <= 16000
            title_spans = [s for s in value.source_spans if s.block_id == documents[0].source_spans[0].block_id]
            assert title_spans and all(s.role == ("source" if index == 0 else "context_prefix") for s in title_spans)
            body_spans = [s for s in value.source_spans if s.block_id == documents[1].source_spans[0].block_id]
            assert body_spans and all(s.role == "source" and s.location == {"element": 2, "slide": 1} for s in body_spans)
            assert not any(token.type in {"code_block", "fence"} for token in MarkdownIt("commonmark").parse(value.content))
    assert not any(draft.attachments for draft in drafts)
