"""Local text parsing regressions: content loss, unsafe output and provenance."""

from __future__ import annotations

import asyncio
import signal
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from actweave_knowledge.extraction.contracts import AttachmentOccurrence, Document, ExtractionError, SourceSpan
from actweave_knowledge.extraction.processor import ExtractProcessor
from parsing_test_helpers import make_context, make_document, make_setting


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-be", "gb18030"])
def test_text_decode_is_lossless(tmp_path, encoding):
    from actweave_knowledge.extraction.encoding import decode_text_file

    path = tmp_path / "sample.txt"
    text = "网络接口 编号00123\n中文，不能丢失。" * 10
    raw = text.encode(encoding)
    if encoding == "utf-16-be":
        raw = b"\xfe\xff" + raw
    path.write_bytes(raw)
    decoded, selected, warnings = decode_text_file(path)
    assert decoded == text
    assert selected
    assert bool(warnings) == (encoding == "gb18030")
    assert all(w.code == "ENCODING_DETECTED" for w in warnings)


@pytest.mark.parametrize("payload", [b"\xef\xbb", b"\xff", b"\xff\xfeA", b"\xff\xfe\x00\x00A\x00\x00\x00", b"\x00\x00\xfe\xff\x00\x00\x00A"])
def test_broken_or_unsupported_bom_is_not_reinterpreted(tmp_path, payload):
    from actweave_knowledge.extraction.encoding import decode_text_file

    path = tmp_path / "private-name.txt"
    path.write_bytes(payload)
    with pytest.raises(ExtractionError) as raised:
        decode_text_file(path)
    assert raised.value.reason_code == "TEXT_DECODING_FAILED"
    assert "private-name" not in str(raised.value)


def test_detection_does_not_hide_invalid_tail_after_sample(tmp_path, monkeypatch):
    import actweave_knowledge.extraction.encoding as encoding

    path = tmp_path / "tail.txt"
    path.write_bytes(("网络" * 300000).encode("gb18030") + b"\x81")
    monkeypatch.setattr(encoding, "from_bytes", lambda sample: SimpleNamespace(best=lambda: SimpleNamespace(encoding="gb18030")))
    with pytest.raises(ExtractionError, match="文件解析失败"):
        encoding.decode_text_file(path)


def test_detector_bounds_sample_and_restores_real_timer(monkeypatch):
    import actweave_knowledge.extraction.encoding as encoding

    original_handler = signal.getsignal(signal.SIGALRM)
    original_timer = signal.getitimer(signal.ITIMER_REAL)

    def sentinel(signum, frame):
        return None

    seen = []

    def detect(sample):
        seen.append((len(sample), signal.getitimer(signal.ITIMER_REAL)[0]))
        return SimpleNamespace(best=lambda: SimpleNamespace(encoding="gb18030"))

    signal.signal(signal.SIGALRM, sentinel)
    signal.setitimer(signal.ITIMER_REAL, 30, 40)
    monkeypatch.setattr(encoding, "from_bytes", detect)
    try:
        assert encoding.detect_encoding(b"x" * (1024 * 1024 + 99)) == "gb18030"
        assert seen[0][0] == 1024 * 1024
        assert 0 < seen[0][1] <= 5
        assert signal.getsignal(signal.SIGALRM) is sentinel
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 29 < remaining <= 30 and interval == 40
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, original_handler)
        signal.setitimer(signal.ITIMER_REAL, *original_timer)


@pytest.mark.parametrize("mode", ["empty", "failure", "timeout"])
def test_detection_failures_are_safe_and_restore_signal(monkeypatch, mode):
    import actweave_knowledge.extraction.encoding as encoding

    previous = signal.getsignal(signal.SIGALRM)

    def detect(sample):
        if mode == "failure":
            raise ValueError("private path and bytes")
        if mode == "timeout":
            signal.raise_signal(signal.SIGALRM)
        return SimpleNamespace(best=lambda: None)

    monkeypatch.setattr(encoding, "from_bytes", detect)
    with pytest.raises(ExtractionError) as raised:
        encoding.detect_encoding(b"\x81")
    assert raised.value.reason_code == "TEXT_DECODING_FAILED"
    assert "private" not in str(raised.value)
    assert signal.getsignal(signal.SIGALRM) is previous
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_detection_rejects_thread_and_async_loop_without_touching_signal():
    from actweave_knowledge.extraction.encoding import detect_encoding

    def attempt():
        with pytest.raises(ExtractionError) as raised:
            detect_encoding(b"\x81")
        assert raised.value.reason_code == "TEXT_DECODING_FAILED"

    assert threading.current_thread() is threading.main_thread()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(attempt).result()

    async def in_loop():
        attempt()

    asyncio.run(in_loop())


def test_source_size_limit_is_enforced_during_read(tmp_path):
    from actweave_knowledge.contracts import KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError
    from actweave_knowledge.extraction.encoding import decode_text_file

    path = tmp_path / "large.txt"
    with path.open("wb") as file:
        file.truncate(50 * 1024 * 1024 + 1)
    with pytest.raises(KnowledgeError) as raised:
        decode_text_file(path)
    assert raised.value.code == KNOWLEDGE_QUOTA_EXCEEDED


def test_text_preserves_whitespace_and_proves_original_line_locations(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"  first\r\n\rsecond  \n")
    docs = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert len(docs) == 1
    assert docs[0].page_content == "  first\n\nsecond  \n"
    assert [(s.block_id, s.location["line"], docs[0].page_content[s.start : s.end]) for s in docs[0].source_spans] == [("line:1", 1, "  first\n"), ("line:2", 2, "\n"), ("line:3", 3, "second  \n")]
    assert all(s.location["encoding"] == "utf-8" for s in docs[0].source_spans)


def test_markdown_keeps_generics_hash_fences_and_ancestor_paths(tmp_path):
    from actweave_knowledge.extraction.normalizer import normalize_documents

    path = tmp_path / "sample.md"
    text = "# C#\n父说明\n## 子节\nList<int> Map<K,V> <IP>\n```cpp\nvector<int> x;\n```\n"
    path.write_text(text, encoding="utf-8")
    docs = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert [d.heading_path for d in docs] == [("C#",), ("C#", "子节")]
    assert "".join(d.page_content for d in docs) == text
    assert normalize_documents(normalize_documents(docs)) == docs
    assert docs[1].page_content[docs[1].source_spans[1].start : docs[1].source_spans[1].end] == "List<int> Map<K,V> <IP>\n"
    assert docs[1].source_spans[0].block_id == "line:3"
    assert docs[1].source_spans[0].location["line"] == 3


@pytest.mark.parametrize("fence,closing", [("   ~~~~python", "   ~~~~~"), ("```", "```"), ("````", "`````"), ("~~~", "")])
def test_markdown_fences_hide_headings_and_keep_literals(fence, closing):
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    text = f"# 主\n{fence}\n## literal\n![literal](https://no.invalid/x)\n{closing}\n"
    docs = markdown_sections(text)
    assert len(docs) == 1
    assert docs[0].heading_path == ("主",)
    assert docs[0].page_content == text


def test_short_or_wrong_fence_does_not_end_code_block():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    text = "# Header\n````js\n```\n~~~\n# code\n````\n## End\n"
    docs = markdown_sections(text)
    assert [d.heading_path for d in docs] == [("Header",), ("Header", "End")]
    assert "# code" in docs[0].page_content


def test_mdx_and_inline_code_stay_literal_without_execution(tmp_path):
    path = tmp_path / "sample.mdx"
    text = "## C# ###\n<Component value={dangerous()}/>\n{process.exit(1)}\n`List<int> ![x](https://x)`\n"
    path.write_text(text)
    docs = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert docs[0].page_content == text
    assert docs[0].heading_path == ("C#",)
    assert not docs[0].warnings


def test_external_images_have_visible_alt_and_synthetic_spans_without_network(monkeypatch):
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    def blocked(*args, **kwargs):
        pytest.fail("parser attempted network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    text = "前![拓扑图](https://tracker.invalid/x)后\n![另一张][pic]\n\n[pic]: https://tracker.invalid/y\n"
    docs = markdown_sections(text)
    doc = docs[0]
    assert "拓扑图" in doc.page_content and "另一张" in doc.page_content
    assert "![" not in doc.page_content
    assert len(doc.warnings) == 2
    assert all(w.code == "EXTERNAL_IMAGE_NOT_FETCHED" for w in doc.warnings)
    synthetic = [s for s in doc.source_spans if s.role == "context_prefix"]
    assert len(synthetic) == 2
    assert [s.location["line"] for s in synthetic] == [1, 2]
    assert all("外部图片未获取" in doc.page_content[s.start : s.end] for s in synthetic)
    source = [doc.page_content[s.start : s.end] for s in doc.source_spans if s.role == "source"]
    assert source[:2] == ["前", "后\n"]
    assert not doc.attachments


def test_normalization_remaps_actual_attachment_occurrences_without_merging():
    from actweave_knowledge.extraction.normalizer import normalize_documents

    ref = "a" * 64
    image = f"![one](knowledge-attachment:{ref})"
    text = f"before\r\n{image}\r\n{image}"
    first = SourceSpan(block_id="image:1", start=8, end=8 + len(image), location={"paragraph": 2})
    second = SourceSpan(block_id="image:2", start=10 + len(image), end=len(text), location={"paragraph": 3})
    doc = Document(
        page_content=text, source_spans=(SourceSpan(block_id="text", start=0, end=len(text)),), attachments=(AttachmentOccurrence(ref=ref, alt_text="one", source=first), AttachmentOccurrence(ref=ref, alt_text="one", source=second))
    )
    normalized = normalize_documents([doc])[0]
    assert normalized.page_content == f"before\n{image}\n{image}"
    assert len(normalized.attachments) == 2
    assert all(normalized.page_content[a.source.start : a.source.end] == image for a in normalized.attachments)
    assert normalize_documents([normalized]) == [normalized]


def test_normalizer_is_identity_for_unchanged_spans_and_literals():
    from actweave_knowledge.extraction.normalizer import normalize_documents

    doc = make_document("  C# List<int> <IP>\n```c++\nMap<K,V>\n```\n")
    assert normalize_documents([doc]) == [doc]


def test_html_drops_active_content_but_keeps_code_link_label_and_image_alt():
    from actweave_knowledge.extraction.dify.html_extractor import html_to_documents

    docs = html_to_documents(
        '<h1>标题</h1><script>SECRET()</script><style>HIDDEN</style><iframe>HIDDEN</iframe><pre>List&lt;int&gt;</pre><a href="javascript:alert(1)" onclick="SECRET()">查看</a><img src="https://tracker.invalid/x" alt="拓扑图">'
    )
    text = "\n".join(d.page_content for d in docs)
    assert "List<int>" in text and "查看" in text and "拓扑图" in text
    assert all(term not in text for term in ("SECRET", "HIDDEN", "javascript:", "https://tracker.invalid", "onclick"))
    assert any(w.code == "EXTERNAL_IMAGE_NOT_FETCHED" for d in docs for w in d.warnings)
    assert all("page" not in s.location for d in docs for s in d.source_spans)
    assert all(s.block_id.startswith("html:") and s.location["element"] >= 1 for d in docs for s in d.source_spans)
    placeholders = [(d, s) for d in docs for s in d.source_spans if s.role == "context_prefix"]
    assert len(placeholders) == 1
    doc, span = placeholders[0]
    assert "外部图片未获取" in doc.page_content[span.start : span.end]


def test_html_preserves_heading_list_table_code_and_safe_links_order():
    from actweave_knowledge.extraction.dify.html_extractor import html_to_documents

    docs = html_to_documents(
        '<h1>主</h1><h2>次</h2><ol start="3"><li>A<ul><li>B</li></ul></li><li>C</li></ol>'
        "<table><tr><th>键</th><th>值</th></tr><tr><td>x</td><td>00123</td></tr></table>"
        '<p><code>Map&lt;K,V&gt;</code> <a href="https://example.com/a?q=1">网站</a></p>'
    )
    text = "\n".join(d.page_content for d in docs)
    for literal in ("# 主", "## 次", "3. A", "- B", "4. C", "| 键 | 值 |", "| x | 00123 |", "`Map<K,V>`", "https://example.com/a?q=1"):
        assert literal in text
    assert text.index("3. A") < text.index("| 键") < text.index("Map<K,V>")
    assert docs[-1].heading_path == ("主", "次")
    for doc in docs:
        assert "".join(doc.page_content[s.start : s.end] for s in doc.source_spans) == doc.page_content


def test_html_honors_declared_encoding_and_uses_same_adapter(tmp_path):
    from actweave_knowledge.extraction.dify.html_extractor import html_to_documents

    markup = '<html><head><meta charset="gb18030"></head><body><p>中文，网络接口</p></body></html>'.encode("gb18030")
    path = tmp_path / "test.html"
    path.write_bytes(markup)
    docs = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert docs == html_to_documents(markup)
    assert "中文，网络接口" in "".join(d.page_content for d in docs)


@pytest.mark.parametrize("url", ["data:text/html,hi", "file:///etc/passwd", "javascript:alert(1)", "java\nscript:alert(1)"])
def test_html_unsafe_links_retain_only_label(url):
    from actweave_knowledge.extraction.dify.html_extractor import html_to_documents

    docs = html_to_documents(f'<p><a href="{url}">Visible</a></p>')
    assert docs[0].page_content == "Visible"


def test_line_provenance_uses_physical_newlines_not_unicode_separators(tmp_path):
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    text = "a\u2028b\vstill first\nnext\n"
    path = tmp_path / "physical.txt"
    path.write_text(text)
    plain = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))[0]
    markdown = markdown_sections(text)[0]
    for doc in (plain, markdown):
        assert [(s.location["line"], doc.page_content[s.start : s.end]) for s in doc.source_spans] == [(1, "a\u2028b\vstill first\n"), (2, "next\n")]


def test_cross_section_reference_images_and_inline_literals_are_distinguished():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections
    from actweave_knowledge.extraction.normalizer import normalize_documents

    docs = markdown_sections("# First\n![Topology][ref]\n# Later\n`![literal][ref]`\n\n[ref]: https://example.invalid/image\n")
    assert "外部图片未获取：Topology" in docs[0].page_content
    assert "`![literal][ref]`" in docs[1].page_content
    assert len(docs[0].warnings) == 1 and not docs[1].warnings
    assert normalize_documents(docs) == docs


def test_external_image_with_markdown_alt_is_idempotent():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections
    from actweave_knowledge.extraction.normalizer import normalize_documents

    docs = markdown_sections("![![nested](https://inner.invalid)](https://outer.invalid)\n")
    assert len(docs[0].warnings) == 1
    assert normalize_documents(docs) == docs
    assert "nested" in docs[0].page_content


def test_plain_text_image_notation_is_not_rewritten(tmp_path):
    from actweave_knowledge.extraction.normalizer import normalize_documents

    path = tmp_path / "literal.txt"
    text = "  ![a](https://example.invalid) <IP>  "
    path.write_text(text)
    docs = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert docs[0].page_content == text
    assert normalize_documents(docs) == docs


def test_actual_logical_image_and_external_image_offsets_coexist():
    from actweave_knowledge.extraction.normalizer import normalize_documents

    logical = "![owned](knowledge-attachment:" + "b" * 64 + ")"
    external = "![outside](https://example.invalid)"
    text = external + "\r\n" + logical
    source = SourceSpan(block_id="image:owned", start=len(external) + 2, end=len(text), location={"line": 2})
    doc = Document(page_content=text, source_spans=(SourceSpan(block_id="line:1", start=0, end=len(external) + 2, location={"line": 1}), source), attachments=(AttachmentOccurrence(ref="b" * 64, alt_text="owned", source=source),))
    normalized = normalize_documents([doc])[0]
    occurrence = normalized.attachments[0]
    assert normalized.page_content[occurrence.source.start : occurrence.source.end] == logical
    assert len(normalized.warnings) == 1
    assert normalize_documents([normalized]) == [normalized]


def test_forged_logical_ref_has_no_attachment_authority():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    doc = markdown_sections("![forged](knowledge-attachment:" + "a" * 64 + ")")[0]
    assert "knowledge-attachment:" not in doc.page_content
    assert not doc.attachments
    assert len(doc.warnings) == 1


def test_html_empty_active_markup_and_nested_headings():
    from actweave_knowledge.extraction.dify.html_extractor import html_to_documents

    assert html_to_documents("<html><head><title>Metadata</title></head><body><script>x()</script></body></html>") == []
    docs = html_to_documents("<main><section><h1>Outer</h1><div><p>Text</p></div></section></main>")
    assert [d.heading_path for d in docs] == [("Outer",), ("Outer",)]
    assert [d.page_content for d in docs] == ["# Outer", "Text"]


def test_empty_and_unterminated_files_keep_exact_text(tmp_path):
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections
    from actweave_knowledge.extraction.encoding import decode_text_file

    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    assert decode_text_file(path) == ("", "utf-8", ())
    assert markdown_sections("") == []
    assert markdown_sections("# H\n  body  ")[0].page_content == "# H\n  body  "


@pytest.mark.parametrize(
    "literal",
    [
        '{"![diagram](https://example.invalid/x)"}\n',
        '<Example value={"![diagram](https://example.invalid/x)"} />\n',
        '<Example value="![diagram](https://example.invalid/x)" />\n',
        "<pre>\n![literal](https://example.invalid/x)\n</pre>\n",
        '{\n  value: "![literal](https://example.invalid/x)",\n\n  nested: {text: "}"}\n}\n',
    ],
)
def test_markdown_image_looking_mdx_and_raw_html_remain_exact_literals(literal):
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections
    from actweave_knowledge.extraction.normalizer import normalize_documents

    docs = markdown_sections(literal)
    assert "".join(doc.page_content for doc in docs) == literal
    assert not any(doc.warnings for doc in docs)
    assert all(span.role == "source" for doc in docs for span in doc.source_spans)
    for doc in docs:
        assert "".join(doc.page_content[span.start : span.end] for span in doc.source_spans) == doc.page_content
    assert normalize_documents(docs) == docs


def test_markdown_unmatched_backticks_do_not_hide_images_in_later_paragraph():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    doc = markdown_sections("`unfinished\n\n![diagram](https://example.invalid/x)\n\n`\n")[0]
    assert doc.page_content == "`unfinished\n\n（外部图片未获取：diagram）\n\n`\n"
    assert len(doc.warnings) == 1
    assert doc.warnings[0].source_position == {"line": 3, "encoding": "utf-8"}
    placeholders = [span for span in doc.source_spans if span.role == "context_prefix"]
    assert len(placeholders) == 1
    span = placeholders[0]
    assert span.block_id == "line:3"
    assert doc.page_content[span.start : span.end] == "（外部图片未获取：diagram）"


def test_markdown_inline_mdx_literal_does_not_hide_adjacent_real_image():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    literal = '{"`![literal](https://example.invalid/literal)"}'
    doc = markdown_sections(literal + " ![real](https://example.invalid/real)\n")[0]
    assert doc.page_content == literal + " （外部图片未获取：real）\n"
    assert len(doc.warnings) == 1
    span = next(s for s in doc.source_spans if s.role == "context_prefix")
    assert doc.page_content[span.start : span.end] == "（外部图片未获取：real）"


def test_markdown_list_and_quote_image_spans_use_original_offsets():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    doc = markdown_sections("> before ![quote](https://example.invalid/q) after\n\n- ![list](https://example.invalid/l)\n")[0]
    assert doc.page_content == "> before （外部图片未获取：quote） after\n\n- （外部图片未获取：list）\n"
    spans = [span for span in doc.source_spans if span.role == "context_prefix"]
    assert [doc.page_content[s.start : s.end] for s in spans] == ["（外部图片未获取：quote）", "（外部图片未获取：list）"]
    assert [s.location["line"] for s in spans] == [1, 3]


@pytest.mark.parametrize(
    "markup,expected",
    [
        ("<blockquote><p>first</p><p>second</p><pre>line 1\nline 2</pre></blockquote>", "> first\n>\n> second\n>\n> ```\n> line 1\n> line 2\n> ```"),
        ("<ul><li><p>first</p><p>second</p></li></ul>", "- first\n  \n  second"),
        ("<figure><pre>line 1\nline 2</pre></figure>", "```\nline 1\nline 2\n```"),
        ('<ol start="2"><li><p>first</p><pre>line 1\nline 2</pre></li></ol>', "2. first\n   \n   ```\n   line 1\n   line 2\n   ```"),
    ],
)
def test_html_nested_blocks_keep_paragraph_and_code_boundaries(markup, expected):
    from actweave_knowledge.extraction.dify.html_extractor import html_to_documents

    docs = html_to_documents(markup)
    assert "\n".join(doc.page_content for doc in docs) == expected
    for number, doc in enumerate(docs, 1):
        assert "".join(doc.page_content[s.start : s.end] for s in doc.source_spans) == doc.page_content
        assert all(s.location == {"element": number} for s in doc.source_spans)


def test_html_nested_blocks_keep_image_context_spans():
    from actweave_knowledge.extraction.dify.html_extractor import html_to_documents

    docs = html_to_documents('<blockquote><p>before</p><p><img src="https://example.invalid/x" alt="diagram"></p><pre>a\nb</pre></blockquote>')
    assert "\n".join(doc.page_content for doc in docs) == "> before\n>\n> （外部图片未获取：diagram）\n>\n> ```\n> a\n> b\n> ```"
    assert sum(len(doc.warnings) for doc in docs) == 1
    spans = [(doc, span) for doc in docs for span in doc.source_spans if span.role == "context_prefix"]
    assert len(spans) == 1
    doc, span = spans[0]
    assert doc.page_content[span.start : span.end] == "（外部图片未获取：diagram）"
    assert "".join(doc.page_content[s.start : s.end] for s in doc.source_spans) == doc.page_content


def test_markdown_braces_inside_inline_code_do_not_hide_real_image():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    doc = markdown_sections("`{` ![real](https://example.invalid/x) `}`\n")[0]
    assert doc.page_content == "`{` （外部图片未获取：real） `}`\n"
    assert len(doc.warnings) == 1


def test_markdown_multiline_quote_image_keeps_original_location():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    doc = markdown_sections("> before\n> ![two\n> lines](https://example.invalid/x) after\n")[0]
    assert doc.page_content == "> before\n> （外部图片未获取：two lines） after\n"
    assert len(doc.warnings) == 1
    placeholder = next(s for s in doc.source_spans if s.role == "context_prefix")
    assert placeholder.location["line"] == 2
    assert doc.page_content[placeholder.start : placeholder.end] == "（外部图片未获取：two lines）"


def test_markdown_tab_indented_list_continuation_maps_real_image():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections

    doc = markdown_sections("- first\n\tcontinued ![real](https://example.invalid/x)\n")[0]
    assert doc.page_content == "- first\n\tcontinued （外部图片未获取：real）\n"
    span = next(s for s in doc.source_spans if s.role == "context_prefix")
    assert span.location["line"] == 2
    assert doc.page_content[span.start : span.end] == "（外部图片未获取：real）"


@pytest.mark.parametrize(
    "markup,expected",
    [
        ("<p><span>Hello </span><span>world</span></p>", "Hello world"),
        ("<p>first<strong> second </strong>third</p>", "first** second **third"),
        ('<p><a href="https://example.invalid">A </a><span>B</span></p>', "[A ](<https://example.invalid>)B"),
        ("<blockquote><p><span>Hello </span><span>world</span></p><p>next</p></blockquote>", "> Hello world\n>\n> next"),
        ("<ul><li><span>A </span><em>B </em><span>C</span><p>next</p></li></ul>", "- A *B *C\n  \n  next"),
    ],
)
def test_html_inline_sibling_whitespace_survives_block_conversion(markup, expected):
    from actweave_knowledge.extraction.dify.html_extractor import html_to_documents

    docs = html_to_documents(markup)
    assert "\n".join(doc.page_content for doc in docs) == expected
    assert all("".join(doc.page_content[s.start : s.end] for s in doc.source_spans) == doc.page_content for doc in docs)


@pytest.mark.parametrize("alt", ["<IP>", "{topology}", "router <IP> diagram"])
def test_real_markdown_image_alt_is_not_literal_mask_text(alt):
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections
    from actweave_knowledge.extraction.markdown_images import find_markdown_images
    from actweave_knowledge.extraction.normalizer import normalize_documents

    original = f"![{alt}](https://example.invalid/x)"
    (image,) = find_markdown_images(original + "\n")
    assert image.alt_text == alt
    assert original[image.start : image.end] == original
    doc = markdown_sections(original + "\n")[0]
    assert doc.page_content == f"（外部图片未获取：{alt}）\n"
    assert len(doc.warnings) == 1
    span = next(s for s in doc.source_spans if s.role == "context_prefix")
    assert doc.page_content[span.start : span.end] == f"（外部图片未获取：{alt}）"
    assert normalize_documents([doc]) == [doc]


def test_image_mask_does_not_manufacture_bracket_structure_or_change_url():
    from actweave_knowledge.extraction.dify.markdown_extractor import markdown_sections
    from actweave_knowledge.extraction.markdown_images import find_markdown_images

    malformed = "![{a] b}](https://example.invalid/x)\n"
    assert find_markdown_images(malformed) == ()
    assert markdown_sections(malformed)[0].page_content == malformed
    original = "![<IP>](https://example.invalid/{router})"
    (image,) = find_markdown_images(original)
    assert image.source == "https://example.invalid/%7Brouter%7D"
    assert image.alt_text == "<IP>"
    assert original[image.start : image.end] == original
