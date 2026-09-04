# RAG 原文字面量保真与 Word 标题 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现规格 A2/A3：保护非 Markdown 原文字面量、补充 Word 自定义大纲标题，并以真实的 `adapter-v2` 身份隔离旧解析缓存和冻结配置。

**Architecture:** 在 extraction 内增加一个纯函数，只序列化原文文本叶节点；HTML 结构、代码和 Word 图片/链接生成逻辑继续使用现有实现。Word 复用已有 offset remap 保护跨 run 的缩进，再由本地 XML 和样式继承确定一级至六级标题；token 清洗兼容新表示中的邮箱 escape 与缩进实体。沿用 ParseProfile、资源锁、预览指纹和显式重新解析，不新增版本分派器或数据迁移。

**Tech Stack:** Python 3.12、现有 `re`、python-docx 1.2.0、BeautifulSoup、pypdfium2、markdown-it-py、pytest、现有 PostgreSQL 隔离测试 harness。

**Spec:** [2026-09-04-rag-quality-optimization-design.md](/Users/jiangfeng/workspace/deer-flow/docs/superpowers/specs/2026-09-04-rag-quality-optimization-design.md)，重点 §4、§6、§7、§9、A07–A13、A15、A17、A18。本计划只细化 A2/A3 与 Adapter 版本；不扩大候选增强范围。

## Global Constraints

- 本文是实施计划，不是已经执行的结果。仅允许生成计划的授权不包括实现、提交、数据库维护、部署、外部模型调用或修改运行配置。
- 所有任务完整继承 Spec §4 和 §9；执行前读取根 `AGENTS.md`、`backend/AGENTS.md`、`CONTEXT.md` 及 Spec，不以本页代替它们。
- token profile 父段默认 1000、overlap 默认 100；父子模式子块默认 500。父段范围 200..4000、overlap 0..500 且小于父段、子块范围 100..2000 且小于父段，不变；历史 character 值不套用这一 Token 口径。
- token profile 的父段和 Child 分别满足显示 Markdown Token、`index_text` Token 及 16000 字符上限；标题、表头、分隔符和保护字符均参与相关预算。overlap 不是额外赠送的预算。
- 每文档父分段最多 5000，父子模式的累计 Child 向量条目另限 5000；两者不是合计 5000。沿用当前其余配额检查，不增加上限。
- Child 在各自父段内零重叠；不新增跨父段全局去重。冻结 character 算法保持现状，重新向量化不重新读取或切分原文件。
- 预览、摄取和显式重新解析共用现有 extraction 与 `split_documents`；`content` 是显示 Markdown，模型、词法和 Reranker 消费 `index_text`。
- 原文和重复原文保留 `source`；既有标题/字段前缀保留 `context_prefix`。offset 对应最终序列化字符，物理页码/行号/段落号不改写。
- 保留 Project 权限、服务器身份、任务租约、CAS、原子发布、附件闭包及当前配额；不新增依赖、DDL、运行时下载、远程解析、OCR、通用 AST 或历史执行器。
- `ADAPTER_REVISION` 从 `adapter-v1` 改为 `adapter-v2`；`md-v1`、`raster-v1` 和 tokenizer 身份不变。本计划依赖总计划 C1 唯一拥有者将 token cleaner 升至 `cleaner-v2` 并在 token splitter 检查该身份，同时发布 `splitter-v3`；不并写 `profiles.py`。这是规划时组合验证发现的 A2 必需表示兼容细化，不是新清洗开关、已实现结果或对原 Spec 文件的修改。
- F1 首次改变 Adapter 输出时同步 revision 和当前已支持平台的真实资源锁。F2–F5 属于同一尚未发布的 `adapter-v2` 批次；整个批次通过联合门禁后才可提出发布申请，不能将中间版本部署后继续在同一身份下追加行为。
- 目前检查到的资源锁仅登记 `darwin-arm64`；不能据此声称 Linux 已支持或已验证。新目标平台必须提供实际资源核验，不复制或伪造平台条目。
- 用户未提交的 `index_text.py`、`splitter.py`、`tokenizer.py` 及其测试/文档/UI 修改属于用户。只修改此计划明确拥有的文件和局部内容，不 reset、restore、stage、commit、push。
- `README.md`、`backend/AGENTS.md`、`frontend/AGENTS.md` 由总计划集成人员统一修改；本子计划只提供准确文案，不安排多个执行者并写公共文档。
- Word `Title/Subtitle`、自动编号、七至九级标题映射、表格行合并、PDF 版面重建不在本计划内。

---

## 文件与接口所有权

以下源码路径以 `K = backend/packages/knowledge/actweave_knowledge` 为简写；Files 清单均给出完整仓库相对路径。命令默认在 `/Users/jiangfeng/workspace/deer-flow/backend` 执行。测试命令使用已有 `.venv/bin/python`，不会隐式安装依赖；环境未准备好时报告缺失，不用联网安装绕过。

| 文件 | 单一职责/拥有任务 |
| --- | --- |
| `K/extraction/literal.py` | F1：原文字面量纯函数，不接受已生成的 Markdown |
| `K/extraction/builtin/text_extractor.py` | F1：先序列化每条原始行，再分配行 span |
| `K/extraction/runtime_resources.py`、`resources.lock.json` | F1：真实 Adapter 版本与资源身份；F5 只验证 |
| `K/extraction/builtin/word_extractor.py` | F2：文本及缩进；F4：标题，严格串行拥有 |
| `K/extraction/builtin/pdf_extractor.py` | F2：文字层原文保护发生在页 span、图片位置之前 |
| `K/extraction/builtin/html_extractor.py` | F3：叶节点保护及表格 pipe 的单次序列化 |
| `K/extraction/unstructured_local/elements.py` | F3：普通 element/fallback 保护，不动结构表格分支 |
| `K/ingestion/cleaner.py` | F4C：仅 token 清洗兼容邮箱 escape 与缩进实体，不改 raw/character 规则 |
| `backend/tests/knowledge/test_literal_cleaning.py` | F4C：清洗组合与 source/附件 remap 回归 |
| `backend/tests/knowledge/test_literal_markdown.py` | F1 新增，F3 后续顺序追加；共享 helper/TXT/HTML/Unstructured 契约 |
| `backend/tests/knowledge/test_builtin_office_pdf.py` | F2/F4 顺序追加，复用 `_extract`、`_png`、`_hyperlink` |
| `backend/tests/knowledge/test_builtin_text_extractors.py` | F1 更新唯一过时 TXT 原始字节断言，不改 Markdown 断言 |
| `backend/tests/knowledge/test_extraction_resources.py` | F1 新增 v2 包装检查 |
| `backend/tests/knowledge/test_parsing_profiles.py`、`test_extraction_cache.py`、`test_parsing_pipeline.py` | F5：身份、真实隔离数据库/本地对象存储、完整流水线验收 |

唯一新增生产接口：

```python
def escape_literal_text(
    text: str,
    *,
    protect_indentation: bool = True,
    escape_pipes: bool = True,
) -> str:
```

它不是幂等的 Markdown normalizer，只允许对原文调用一次。`protect_indentation=False` 用于 Word（末尾已有 remap）和 HTML（保留自身布局空白处理）；`escape_pipes=False` 仅用于 HTML 表格叶节点，由 `_table()` 现有最终步骤保护 pipe。没有新配置字段。

跨包约束：分段子计划扩展 `inline_atoms(text: str, *, include_text_escapes: bool = False)`；只有 token 切分、预算截取和 overlap 入口传 `include_text_escapes=True`，cleaner 既有调用保留默认语义。不能在 `\-`、`\&amp;` 的 escape 或 `&#32;`/`&#9;` 实体中间切分。本计划不并写 `structure.py`，F5 的长字面量测试作为联合验收。

## F1：共享字面量函数、TXT 与首个版本化交付

**Files:**

- Create: `backend/packages/knowledge/actweave_knowledge/extraction/literal.py`
- Create: `backend/tests/knowledge/test_literal_markdown.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/builtin/text_extractor.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/runtime_resources.py`
- Generate: `backend/packages/knowledge/actweave_knowledge/extraction/resources.lock.json`
- Modify: `backend/tests/knowledge/test_builtin_text_extractors.py`
- Modify: `backend/tests/knowledge/test_extraction_resources.py`

**Interfaces:**

- Consumes: `decode_text_file(path)`、`source_lines(text)`、`SourceSpan`、`Document`、`build_index_text(markdown)`、`make_context(work_dir)`、`make_setting(path)`。
- Produces: `escape_literal_text(text, *, protect_indentation=True, escape_pipes=True) -> str`；以 v2 身份输出且保持原始行来源的 `TextExtractor.extract()`。
- 不改变 `ExtractSetting`、`ParseProfile`、`ProcessingProfile`、normalizer 和任何请求字段。

- [ ] **Step 1（2–5 分钟）：记录基线与拥有文件的变更，确认 revision 尚未被其他任务占用。**

从仓库根运行：

```bash
git status --short
git diff -- backend/packages/knowledge/actweave_knowledge/extraction backend/tests/knowledge/test_builtin_text_extractors.py backend/tests/knowledge/test_builtin_office_pdf.py
rg -n 'ADAPTER_REVISION|NORMALIZATION_VERSION|SPLITTER_VERSION' backend/packages/knowledge/actweave_knowledge/extraction/runtime_resources.py backend/packages/knowledge/actweave_knowledge/ingestion/profiles.py
```

预期：Adapter 当前仍为 `adapter-v1`。若已被并行工作占用，停止修改该常量并与集成人员确认新身份，不覆盖别人的实现。本计划中 `adapter-v2` 的所有期望值需一起更新。

- [ ] **Step 2（2–5 分钟）：新建共享函数的失败测试。**

新建 `backend/tests/knowledge/test_literal_markdown.py`：

```python
"""Plain source text must not acquire Markdown meaning during extraction."""

import pytest
from actweave_knowledge.extraction.literal import escape_literal_text
from actweave_knowledge.ingestion.index_text import (
    build_index_text,
    has_indexable_source_text,
)
from markdown_it import MarkdownIt
from parsing_test_helpers import make_document


LITERALS = (
    "# text", "- item", "+ item", "1. item", "1) item", "> text",
    "---", "text\n===", "```", "a|b", "[x](https://example.invalid)",
    "![x](https://example.invalid)", r"\path", "&amp;", "&#35;",
    "    # indent", "\t# indent", " \t# indent",
)


@pytest.mark.parametrize("raw", LITERALS)
def test_literal_serializer_preserves_visible_text_without_structure(raw):
    rendered = escape_literal_text(raw)
    assert build_index_text(rendered) == raw.strip()
    assert has_indexable_source_text((make_document(rendered),))
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = parser.parse(rendered)
    prohibited = {
        "heading_open", "bullet_list_open", "ordered_list_open",
        "blockquote_open", "table_open", "hr", "fence", "code_block",
    }
    assert not any(token.type in prohibited for token in tokens)
    assert not any(
        child.type in {"image", "link_open"}
        for token in tokens for child in token.children or ()
    )


@pytest.mark.parametrize(
    "pieces",
    [("1", ". item"), ("1", "2. item"), ("1", ") item"),
     ("text\n", "=", "=="), ("-", " item")],
)
def test_fragmented_markers_need_no_mutable_serializer_state(pieces):
    rendered = "".join(
        escape_literal_text(piece, protect_indentation=False)
        for piece in pieces
    )
    assert build_index_text(rendered) == "".join(pieces)


def test_serializer_keeps_ordinary_punctuation_and_supplies_context_options():
    ordinary = "Knowledge parser readiness. SGVsbG8= a-b 10.0.0.1"
    assert escape_literal_text(ordinary) == ordinary
    assert escape_literal_text("a|b", escape_pipes=False) == "a|b"
    assert escape_literal_text("    x", protect_indentation=False) == "    x"
    assert escape_literal_text("    x").startswith("&#32;   x")
    assert escape_literal_text("\tx").startswith("&#9;x")
```

- [ ] **Step 3（2–5 分钟）：运行新测试，确认缺少实现而失败。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_literal_markdown.py -q
```

预期：collection 报 `ModuleNotFoundError: actweave_knowledge.extraction.literal`，不是数据库、网络或依赖安装失败。

- [ ] **Step 4（2–5 分钟）：写最小纯函数，不新增 mutable 状态或 parser。**

`backend/packages/knowledge/actweave_knowledge/extraction/literal.py` 完整内容：

```python
"""Serialize ordinary source text, never already-generated Markdown."""

import re

_INLINE = re.compile(r"([\\`*_\[\]<>#!~&])")
_BLOCK = re.compile(r"(?m)^([ \t]{0,3})([-+=]|[0-9]{0,9}[.)])")
_INDENT = re.compile(r"(?m)^(?= {4}| {0,3}\t)[ \t]")


def escape_literal_text(
    text: str,
    *,
    protect_indentation: bool = True,
    escape_pipes: bool = True,
) -> str:
    text = _INLINE.sub(r"\\\1", text)
    if escape_pipes:
        text = text.replace("|", r"\|")
    # Zero digits protects a delimiter beginning a later Word/HTML leaf.
    text = _BLOCK.sub(lambda match: match[1] + match[2][:-1] + "\\" + match[2][-1], text)
    if protect_indentation:
        # Introduce entities only after escaping source entity-looking text.
        text = _INDENT.sub(lambda match: "&#9;" if match[0] == "\t" else "&#32;", text)
    return text
```

零位数字不是编号识别：它保护被拆成独立叶节点的 `. item`、`) item`；不会改写普通句尾句号或 base64 尾部等号。缩进实体在源 `&` 转义之后生成，避免把自己的保护再次 escape。

- [ ] **Step 5（2–5 分钟）：追加 TXT 真实文件失败测试。**

向新测试文件追加：

```python
@pytest.mark.parametrize("raw", LITERALS)
def test_txt_serializes_each_source_line_before_assigning_offsets(tmp_path, raw):
    from actweave_knowledge.extraction.processor import ExtractProcessor
    from parsing_test_helpers import make_context, make_setting

    path = tmp_path / "literal.txt"
    path.write_text(raw, encoding="utf-8")
    (document,) = ExtractProcessor().extract(
        make_setting(path), make_context(tmp_path / "work")
    )
    assert build_index_text(document.page_content) == raw.strip()
    assert has_indexable_source_text((document,))
    assert "".join(
        document.page_content[span.start:span.end]
        for span in document.source_spans
    ) == document.page_content
    assert [span.location["line"] for span in document.source_spans] == list(
        range(1, len(raw.splitlines()) + 1)
    )
    assert all(span.role == "source" for span in document.source_spans)
    assert not document.attachments and not document.warnings
```

```bash
.venv/bin/python -m pytest tests/knowledge/test_literal_markdown.py -k txt -q
```

预期：`---` 的 index 空字符串、列表标记丢失等断言 FAIL。

- [ ] **Step 6（2–5 分钟）：在同一个未发布工作单元中接线 TXT 并升级 Adapter 常量。**

在 `text_extractor.py` 加 `from ..literal import escape_literal_text`，把 decode 后到 return 的原有方法段替换为：

```python
        text, encoding, warnings = decode_text_file(setting.source_path)
        spans = []
        parts = []
        offset = 0
        for number, line in enumerate(source_lines(text), 1):
            rendered = escape_literal_text(line)
            spans.append(
                SourceSpan(
                    block_id=f"line:{number}",
                    start=offset,
                    end=offset + len(rendered),
                    location={"line": number, "encoding": encoding},
                )
            )
            parts.append(rendered)
            offset += len(rendered)
        context.check_cancelled()
        return [Document(page_content="".join(parts), source_spans=tuple(spans), kind="text", warnings=warnings)]
```

保留方法开头现有 `context.check_cancelled()`。同一步将 `runtime_resources.py` 的定义改为：

```python
ADAPTER_REVISION = "adapter-v2"
```

不能先交付 TXT 新行为、后补版本；也不要修改公共 normalizer、readiness 固定文本或依赖 pin。

- [ ] **Step 7（2–5 分钟）：添加版本/锁一致性测试，并由真实当前平台生成锁。**

向 `test_extraction_resources.py` 追加：

```python
def test_adapter_v2_is_packaged_with_matching_verified_resource_identity(resources):
    from actweave_knowledge.extraction.registry import default_registry

    assert resources.ADAPTER_REVISION == "adapter-v2"
    locked = resources._locked_manifest()
    assert locked is not None
    assert locked["adapter_revision"] == resources.ADAPTER_REVISION
    registrations = default_registry().registrations
    assert registrations
    assert all(":adapter-v2:" in item.extractor_version for item in registrations)
    assert all(item.dependency_probe() is None for item in registrations)
```

先运行，预期旧资源锁与 v2 不符：

```bash
.venv/bin/python -m pytest tests/knowledge/test_extraction_resources.py::test_adapter_v2_is_packaged_with_matching_verified_resource_identity -q
```

随后只运行既有离线生成器：

```bash
.venv/bin/python scripts/build_extraction_resources.py --output packages/knowledge/actweave_knowledge/extraction/resources.lock.json
git diff --stat -- packages/knowledge/actweave_knowledge/extraction/resources.lock.json
```

预期：生成器成功且保持当前真实包版本/资源 hash；若出现意外依赖或资源变更，先核对安装环境，不扩大 pin 或替换其他平台数据。本机不是锁中已有平台时，不删除原平台条目；该平台也必须用实际安装核验完成 v2 后才能作为同一发布批次支持平台。缺少相应机器属于发布验证缺口，不能通过手改 `adapter_revision` 伪造通过。

- [ ] **Step 8（2–5 分钟）：只更新旧 TXT 测试中过时的“Markdown 必须等于原文”断言。**

在 `test_builtin_text_extractors.py::test_plain_text_image_notation_is_not_rewritten` 中，把 `assert docs[0].page_content == text` 替换为：

```python
    from actweave_knowledge.ingestion.index_text import build_index_text

    assert build_index_text(docs[0].page_content) == text.strip()
    assert not docs[0].attachments and not docs[0].warnings
    assert docs[0].page_content.startswith("  ")
    assert docs[0].page_content.endswith("  ")
```

保留 `assert normalize_documents(docs) == docs`；不改 Markdown 文件、MDX 和代码路径原样保留的其他断言。

- [ ] **Step 9（2–5 分钟）：验证 F1 并做局部审查交付。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_literal_markdown.py tests/knowledge/test_builtin_text_extractors.py tests/knowledge/test_extraction_resources.py -q
git diff --check
```

预期：全部 PASS，零 skip 被当成通过。审查新行为、revision、真实资源锁同时存在；F1 可独立评审，但不能单独发布尚不完整的 v2 批次。提交仅另行授权，不执行 `git add`/`git commit`。

## F2：Word/PDF 叶节点、跨 run 缩进和图片 offset

**Files:**

- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/builtin/word_extractor.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/builtin/pdf_extractor.py`
- Test: `backend/tests/knowledge/test_builtin_office_pdf.py`

**Interfaces:**

- Consumes: F1 `escape_literal_text(text, *, protect_indentation=True, escape_pipes=True)`；Word 现有 `_Block.protect_indentation(state)` 与 `_Block.extend()`；PDF 现有页 span / `_extract_images()`。
- Produces: Word `_literal(text: str) -> str` 仍保留同名私有接口；PDF 输出在分配 page/图片 offset 前已保护。
- Word 正文、表格单元格、hyperlink/legacy field 显示 run 都经现有 `process_run` 路径；不在每个调用点重复过滤。

- [ ] **Step 1（2–5 分钟）：加入真实 Word/PDF 字面文本失败测试。**

追加到 `test_builtin_office_pdf.py`：

```python
@pytest.mark.parametrize("extension", [".docx", ".pdf"])
@pytest.mark.parametrize(
    "raw", ["- item", "+ item", "1. item", "1) item", "---", "&amp;", "    # indent"]
)
def test_word_pdf_plain_source_is_not_markdown(tmp_path, extension, raw):
    from actweave_knowledge.ingestion.index_text import build_index_text, has_indexable_source_text

    path = tmp_path / ("literal" + extension)
    if extension == ".docx":
        word = WordFile()
        word.add_paragraph(raw)
        word.save(path)
    else:
        write_pdf(path, [raw])
    docs, _ = _extract(path, tmp_path / "work")
    assert build_index_text(docs[0].page_content) == raw.strip()
    assert has_indexable_source_text(tuple(docs))
    assert docs[0].source_spans[0].start == 0
    assert docs[0].source_spans[0].end == len(docs[0].page_content)
    expected_location = {"paragraph": 1} if extension == ".docx" else {"page": 1}
    assert docs[0].source_spans[0].location == expected_location
```

- [ ] **Step 2（2–5 分钟）：追加 Word 跨 run、链接文字及图片 offset 失败测试。**

```python
@pytest.mark.parametrize("indent", ["    ", "\t", " \t"])
def test_word_cross_run_literals_and_indent_keep_images_and_positions(tmp_path, indent):
    from actweave_knowledge.ingestion.index_text import build_index_text

    path = tmp_path / "runs.docx"
    png = tmp_path / "red.png"
    _png(png)
    word = WordFile()
    paragraph = word.add_paragraph()
    paragraph.add_run("1")
    paragraph.add_run(". item &amp;\n")
    paragraph.add_run(indent[:1])
    paragraph.add_run(indent[1:])
    paragraph.add_run("# after ")
    paragraph.add_run().add_picture(str(png))
    paragraph.add_run(" tail\n")
    _hyperlink(paragraph, "- linked &amp;", "https://example.invalid/docs")
    word.save(path)

    (doc,), sink = _extract(path, tmp_path / "work")
    indexed = build_index_text(doc.page_content)
    assert indexed.startswith("1. item &amp;\n" + indent + "# after ")
    assert indexed.endswith("tail\n- linked &amp;")
    assert "https://example.invalid/docs" not in indexed
    assert "\\#" not in indexed and "\\&" not in indexed
    assert len(doc.attachments) == len(sink.assets) == 1
    image = doc.attachments[0]
    assert doc.page_content[image.source.start:image.source.end] == f"![图片](knowledge-attachment:{image.ref})"
    assert image.source.location == {"paragraph": 1, "image_index": 1}
    assert doc.source_spans[0].end == len(doc.page_content)
```

`_extract()` 自带 `encode_manifest`/`decode_manifest` round-trip，因此此测试也检查图片 ref 与 occurrence 的位置闭包。

- [ ] **Step 3（2–5 分钟）：证明当前 Word/PDF 仍失败。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_builtin_office_pdf.py -k 'plain_source_is_not_markdown or cross_run_literals' -q
```

预期：列表/分隔线/实体断言失败；不能将 PDFium 提取原文件自身不提供的字符视为本 helper 的成功。

- [ ] **Step 4（2–5 分钟）：替换 Word/PDF 原文序列化，保留图片分支。**

两个 Adapter 各增加：

```python
from ..literal import escape_literal_text
```

Word `_literal` 的完整替换：

```python
def _literal(text: str) -> str:
    return escape_literal_text(text, protect_indentation=False)
```

Word `_Block.protect_indentation()` 中只改现有 `re.finditer` 的 pattern，其余 offset、图片 remap 与配额 charge 原样保留：

```python
        edits = [(match.start(), "&#9;" if match[0] == "\t" else "&#32;") for match in re.finditer(r"(?m)^(?= {4}| {0,3}\t)[ \t]", self.text)]
```

这保留四空格/Tab 的原有处理，并覆盖空格后 Tab；`process_run` 中的 `w:tab`、`w:br`、`w:cr` 仍走现有路径。最后保护发生在跨 run 拼接之后，不能对每个缩进碎片提前生成实体而改变缩进顺序。

PDF `parse()` 中现有原文转义的 `content = re.sub` 赋值整句替换为：

```python
                        content = escape_literal_text(content)
```

移除 PDF 文件不再使用的 `import re`；其后 `total += len(content)`、page span 和 `_extract_images()` 顺序不变。Word 与 PDF 的生成链接、标题、表格和图片字符串不进入 helper。

- [ ] **Step 5（2–5 分钟）：运行 Office/PDF 回归并审查输出位置。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_builtin_office_pdf.py tests/knowledge/test_extraction_images.py tests/knowledge/test_literal_markdown.py -q
git diff --check
```

预期：PASS；既有嵌套表格、重复图片、合并单元格、CMYK/透明图片、资源上限断言不退化。只做审查交付，不提交、不发布。F4 后续写同一个 Word 文件，不与本任务并行。

## F3：HTML 结构旁路、表格单次 pipe 保护与 Unstructured

**Files:**

- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/builtin/html_extractor.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/unstructured_local/elements.py`
- Test: `backend/tests/knowledge/test_literal_markdown.py`
- Existing regression: `backend/tests/knowledge/test_builtin_text_extractors.py`、`backend/tests/knowledge/test_local_unstructured.py`

**Interfaces:**

- Consumes: F1 helper；现有 `Parts = list[tuple[str, bool]]` 与 `html_to_documents(markup)`；`element.text/category/metadata`。
- Produces: `html_to_documents` 公共签名不变；私有 `_inline/_flow_chunks/_flow_parts/_block_parts/_list` 增加 keyword-only `table_cell: bool = False`，只在 HTML 内部传播。
- `table_cell=True` 不关闭其他字符保护，只让原文 pipe 由 `_table` 现有最终 `.replace("|", "\\|")` 处理一次；真实 code/pre 分支不调用 helper。

- [ ] **Step 1（2–5 分钟）：新增 HTML 与 Unstructured 失败矩阵。**

向 `test_literal_markdown.py` 追加：

```python
@pytest.mark.parametrize("raw", LITERALS[:15])
def test_html_text_leaves_preserve_literal_meaning(raw):
    from html import escape
    from actweave_knowledge.extraction.builtin.html_extractor import html_to_documents

    markup = "<p>" + escape(raw).replace("\n", "<br>") + "</p>"
    (document,) = html_to_documents(markup)
    assert build_index_text(document.page_content) == raw
    assert has_indexable_source_text((document,))
    assert "".join(document.page_content[s.start:s.end] for s in document.source_spans) == document.page_content


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
    assert {warning.code for warning in document.warnings} == (
        {"TABLE_STRUCTURE_UNAVAILABLE"} if category == "Table" else set()
    )
```

- [ ] **Step 2（2–5 分钟）：新增真实 HTML 结构与 pipe 单次处理测试。**

```python
def test_html_structure_and_code_bypass_literal_serializer():
    from actweave_knowledge.extraction.builtin.html_extractor import html_to_documents

    markup = (
        '<h2>Heading</h2><p>1<span>. item</span></p>'
        '<ol start="3"><li>listed</li></ol>'
        '<blockquote><p>quoted</p></blockquote>'
        '<p><a href="https://example.invalid/docs">label &amp;amp;</a></p>'
        '<pre>---\n&amp;amp;\n![x](url)</pre>'
        '<p><code>&amp;amp; | [x](url)</code></p>'
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

    (document,) = html_to_documents(
        '<table><tr><th>key</th><th>value</th></tr>'
        '<tr><td><p>a|b &amp;amp;</p></td><td><code>a\\|b</code></td></tr>'
        '<tr><td>c\\|d</td><td><strong>x|y</strong></td></tr></table>'
    )
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(document.page_content)
    assert sum(token.type == "table_open" for token in tokens) == 1
    assert sum(token.type == "td_open" for token in tokens) == 4
    assert build_index_text(document.page_content).splitlines() == [
        "key", "value", "a|b &amp;", "a\\|b", "c\\|d", "x|y",
    ]
    assert "".join(document.page_content[s.start:s.end] for s in document.source_spans) == document.page_content
```

- [ ] **Step 3（2–5 分钟）：运行新增 HTML/Unstructured 测试。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_literal_markdown.py -k 'html or unstructured' -q
```

预期：字面标题、列表、实体等 FAIL；已有原生结构保护测试可先 PASS，不能为了制造 RED 改坏测试。

- [ ] **Step 4（2–5 分钟）：接线 HTML 原文函数和私有上下文签名。**

在 HTML 文件增加 `from ..literal import escape_literal_text`，替换 `_literal`：

```python
def _literal(text: str, *, escape_pipes: bool = True) -> str:
    return escape_literal_text(
        re.sub(r"\s+", " ", text),
        protect_indentation=False,
        escape_pipes=escape_pipes,
    )
```

只修改以下函数定义行，保留全部未列出的现有函数体：

```python
def _inline(node, *, table_cell: bool = False) -> Parts:
def _flow_chunks(container, *, table_cell: bool = False):
def _flow_parts(container, *, table_cell: bool = False) -> Parts:
def _block_parts(node: Tag, *, table_cell: bool = False) -> Parts:
def _list(node: Tag, *, table_cell: bool = False) -> Parts:
```

这些是精确替换的签名行，不是五个空函数。`Parts`、`html_to_documents` 和 `_table` 的定义不改。

- [ ] **Step 5（2–5 分钟）：贯通 `_inline` 与 flow 中的上下文，保证普通叶节点只调用一次 helper。**

将 `_inline` 的 `NavigableString` 返回和递归分支分别替换为：

```python
        return [(_literal(str(node), escape_pipes=not table_cell), False)]
```

```python
        parts = _flow_parts(node, table_cell=table_cell)
```

```python
        parts = [part for child in node.children for part in _inline(child, table_cell=table_cell)]
```

`_flow_chunks` 内原有 `parts = _block_parts(child)`、`pending.extend(_inline(child))` 替换为：

```python
            parts = _block_parts(child, table_cell=table_cell)
```

```python
            pending.extend(_inline(child, table_cell=table_cell))
```

`_flow_parts` 内迭代句替换为：

```python
    for kind, chunk in _flow_chunks(container, table_cell=table_cell):
```

不改变 `_inline` 内 `code` 的 fence 生成、`a` 的安全链接和 `strong/em` 的结构标记。

- [ ] **Step 6（2–5 分钟）：贯通 block/list 的上下文，表格最后只保护一次 pipe。**

`_block_parts` 内 list、blockquote、heading、默认 flow 的对应 return 分别替换为：

```python
        return _list(node, table_cell=table_cell)
```

```python
        return _prefix_lines(_flow_parts(node, table_cell=table_cell), "> ", "> ")
```

```python
        return [("#" * int(node.name[1]) + " ", False), *_flow_parts(node, table_cell=table_cell)]
```

```python
    return _flow_parts(node, table_cell=table_cell)
```

`_list` 内唯一 `_prefix_lines` 调用替换为：

```python
        parts.extend(_prefix_lines(_flow_parts(item, table_cell=table_cell), marker, " " * len(marker)))
```

`_table` 内处理单元格 parts 的句子替换为：

```python
            parts.extend((text.replace("|", "\\|").replace("\n", " "), synthetic) for text, synthetic in _trim(_inline(cell, table_cell=True)))
```

`pre`、`hr`、嵌套 `_table(node)` 分支及顶层 `visit()` 默认调用保持不变；code/pre 不进 helper，cell 原文 `a|b` 和原文 `a\|b` 分别在最终步骤得到正确保护，不用不可靠的“前一个字符是否是反斜杠”判定。

- [ ] **Step 7（2–5 分钟）：接线 Unstructured 普通分支，不误改 Title metadata 或 HTML Table。**

在 `elements.py` 增加：

```python
from ..literal import escape_literal_text
```

紧邻最终 `documents.append` 调用之前、`category == "Table"` 分支之后插入：

```python
        text = escape_literal_text(text)
```

该位置保证 Title 的 `heading_path` 已使用原文，结构 Table 分支已在 `continue` 前完成 HTML 转换；只有普通文本和没有结构的 fallback 转义一次。最终 `SourceSpan.end=len(text)` 自然对应序列化文本，不另增 remap。

- [ ] **Step 8（2–5 分钟）：验证 HTML、Unstructured 与原生 Markdown 旁路。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_literal_markdown.py tests/knowledge/test_builtin_text_extractors.py tests/knowledge/test_local_unstructured.py -q
rg -n 'escape_literal_text' packages/knowledge/actweave_knowledge/extraction
git diff --check
```

预期：PASS；helper 调用只来自本计划拥有的非 Markdown 叶节点路径，不出现在 `markdown_extractor.py`、`unstructured_markdown_extractor.py`、`normalizer.py`。保留原生 `.md/.mdx` 内容、fence、真实图片规范化和拒绝 MDX 执行的原有测试。不单独发布本任务。

## F4：Word 自定义大纲级别与样式链

**Files:**

- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/builtin/word_extractor.py`
- Test: `backend/tests/knowledge/test_builtin_office_pdf.py`

**Interfaces:**

- Consumes: `Paragraph._p`、`Paragraph.style`、`style.element`、`style.base_style`、`style.style_id`、`qn()`；F2 保留的 `_literal()` 和 `_State.headings`。
- Produces: 私有 `_heading_level(paragraph: Paragraph) -> int | None`，仅返回 1..6；其余显式级别停止继承并返回 `None`。
- 与 F2 串行执行。内置 `Heading 1–6` 首先按现有名字识别；其它样式依次取段落、本样式、继承链最近显式值。

- [ ] **Step 1（2–5 分钟）：添加 Word 大纲 fixture helper 与核心失败测试。**

向 Office/PDF 测试文件追加：

```python
def _set_outline(element, value):
    properties = element.get_or_add_pPr()
    node = OxmlElement("w:outlineLvl")
    node.set(qn("w:val"), str(value))
    properties.append(node)


@pytest.mark.parametrize(
    "paragraph_level,style_level,expected",
    [(None, None, 2), (None, 2, 3), (0, 2, 1), (9, 2, None),
     (None, 6, None), (None, 8, None), (None, "invalid", None)],
)
def test_word_custom_outline_uses_nearest_explicit_supported_value(
    tmp_path, paragraph_level, style_level, expected
):
    from docx.enum.style import WD_STYLE_TYPE
    from actweave_knowledge.ingestion.index_text import build_index_text

    word = WordFile()
    parent = word.styles.add_style("ReportParent", WD_STYLE_TYPE.PARAGRAPH)
    parent.base_style = word.styles["Heading 2"]
    child = word.styles.add_style("ReportChild", WD_STYLE_TYPE.PARAGRAPH)
    child.base_style = parent
    if style_level is not None:
        _set_outline(parent.element, style_level)
    paragraph = word.add_paragraph("# Literal &amp;", style=child)
    if paragraph_level is not None:
        _set_outline(paragraph._p, paragraph_level)
    word.add_paragraph("following text")
    path = tmp_path / "outline.docx"
    word.save(path)
    docs, _ = _extract(path, tmp_path / "work")
    assert build_index_text(docs[0].page_content) == "# Literal &amp;"
    if expected is None:
        assert not docs[0].page_content.startswith("#")
        assert docs[0].heading_path == docs[1].heading_path == ()
    else:
        assert docs[0].page_content.startswith("#" * expected + " ")
        assert docs[0].heading_path == docs[1].heading_path == ("# Literal &amp;",)
```

- [ ] **Step 2（2–5 分钟）：添加样式环、普通样式和旧标题行为测试。**

```python
def test_word_heading_fallback_terminates_cycles_and_keeps_builtin_priority(tmp_path):
    from docx.enum.style import WD_STYLE_TYPE

    word = WordFile()
    first = word.styles.add_style("CycleFirst", WD_STYLE_TYPE.PARAGRAPH)
    second = word.styles.add_style("CycleSecond", WD_STYLE_TYPE.PARAGRAPH)
    first.base_style = second
    second.base_style = first
    word.add_paragraph("cycle text", style=first)
    word.add_paragraph("title text", style="Title")
    word.add_paragraph("subtitle text", style="Subtitle")
    built = word.add_paragraph("known", style="Heading 1")
    _set_outline(built._p, 9)
    body = word.add_paragraph("body text")
    _set_outline(body._p, 8)
    path = tmp_path / "cycle.docx"
    word.save(path)
    docs, _ = _extract(path, tmp_path / "work")
    assert [doc.page_content for doc in docs[:3]] == ["cycle text", "title text", "subtitle text"]
    assert all(doc.heading_path == () for doc in docs[:3])
    assert docs[3].page_content == "# known"
    assert docs[4].heading_path == ("known",)
    assert docs[4].page_content == "body text"
```

- [ ] **Step 3（2–5 分钟）：确认自定义标题用例先失败。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_builtin_office_pdf.py -k 'custom_outline or heading_fallback' -q
```

预期：当前仅名字匹配时，需继承或直接 outline 的用例 FAIL；显式 unsupported 和普通样式用例可先 PASS。

- [ ] **Step 4（2–5 分钟）：增加私有大纲 helper。**

放在 Word `_safe_url` 前后同一私有 helper 区域，不新增文件：

```python
def _heading_level(paragraph: Paragraph) -> int | None:
    style = paragraph.style
    builtin = re.fullmatch(r"Heading ([1-6])", style.name if style is not None else "")
    if builtin:
        return int(builtin[1])
    nodes = paragraph._p.xpath("./w:pPr/w:outlineLvl")
    seen: set[str] = set()
    while True:
        if nodes:
            try:
                value = int(nodes[0].get(qn("w:val"), ""))
            except (TypeError, ValueError):
                return None
            return value + 1 if 0 <= value <= 5 else None
        if style is None or style.style_id in seen:
            return None
        seen.add(style.style_id)
        nodes = style.element.xpath("./w:pPr/w:outlineLvl")
        style = style.base_style
```

最近的显式无效/不支持值终止继承，不能越过它去寻找可用父级；visited 集合只保证有限遍历，不修复文件。

- [ ] **Step 5（2–5 分钟）：在已有段落入口使用级别，保持 heading_path 原文。**

`_parse_cell_paragraph` 开始处，替换旧 `style`/`heading` 判定到标题前缀添加的整个小段：

```python
        paragraph_content = _Block()
        level = _heading_level(paragraph)
        if level is not None:
            state.headings = [(n, title) for n, title in state.headings if n < level]
            state.headings.append((level, paragraph.text))
            paragraph_content.append("#" * level + " ", state)
```

其它 run、链接、图片、表格、offset 流程保持 F2 结果。`paragraph.text` 存入路径，不换成转义后的 `paragraph_content.text`；未识别的新段落不清空前面有效路径。

- [ ] **Step 6（2–5 分钟）：验证 Word 全量 focused 回归并审查。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_builtin_office_pdf.py tests/knowledge/test_literal_markdown.py -q
git diff --check
```

预期：PASS，样式环终止，内置 Heading 1–6 原有断言通过。审查未增加编号、字号或 `Title/Subtitle` 猜测，也未把 7–9 级截成六级。不提交、不发布。

## F4C：原文字面量与已开启邮箱/多余空白清洗的组合兼容

**Files:**

- Modify: `backend/packages/knowledge/actweave_knowledge/ingestion/cleaner.py`
- Create: `backend/tests/knowledge/test_literal_cleaning.py`
- Coordination only: `backend/packages/knowledge/actweave_knowledge/ingestion/profiles.py` 的 cleaner 身份由总计划唯一拥有者同步处理，本任务不并写该文件。

**Interfaces:**

- Consumes: F1 `escape_literal_text()`、`clean_documents()`、`edit_document()` 和现有 `inline_atoms(text)` 的默认保护代码/链接语义。
- Produces: 私有 `_MARKDOWN_EMAIL: re.Pattern[str]` 和 `_MARKDOWN_WHITESPACE_RUNS: re.Pattern[str]`，仅供 `clean_documents`；`_EMAIL`、`_HORIZONTAL_WHITESPACE_RUNS`、`clean_text`、`clean_blocks`、`clean_character_document` 保持原样。
- 用户仍只控制现有 `remove_urls_emails`、`remove_extra_spaces` 开关；只兼容原有 ASCII 邮箱和水平空白规则的新表示，不新增清洗规则或配置。
- 依赖总计划 C1 的 `cleaner-v2` 和 token 入口版本检查；F4C 与 C1 同批交付，旧 cleaner 身份不能执行新代码。

独立内存探针已发现当前组合缺陷：`a\_b@example.test` 只删掉 `_b@example.test`，留下 `a\`；`\+alice@example.test` 留下反斜杠；`&#32;   ` 只压缩后三个空格而留下两个可见空格。此处修复不得通过关闭字面量保护或在索引阶段隐藏残余解决。

- [ ] **Step 1（2–5 分钟）：新增 escaped 邮箱删除与关闭开关测试。**

新建 `test_literal_cleaning.py`：

```python
"""Optional cleaning must understand the literal serializer's output."""

import pytest
from actweave_knowledge.extraction.literal import escape_literal_text
from actweave_knowledge.ingestion.cleaner import clean_documents
from actweave_knowledge.ingestion.index_text import build_index_text
from parsing_test_helpers import make_document


@pytest.mark.parametrize("email", ["a_b@example.test", "+alice@example.test", ".alice@example.test", "a.b+c_d@example-domain.test"])
def test_email_removal_consumes_the_whole_escaped_address(email):
    rendered = escape_literal_text(email)
    source = make_document("before " + rendered + " after")
    assert clean_documents((source,), remove_extra_spaces=False, remove_urls_emails=False) == (source,)
    (cleaned,) = clean_documents((source,), remove_extra_spaces=False, remove_urls_emails=True)
    assert cleaned.page_content == "before  after"
    assert build_index_text(cleaned.page_content) == "before  after"


def test_markdown_email_pattern_does_not_change_raw_or_character_cleaning():
    from actweave_knowledge.ingestion.cleaner import clean_character_document, clean_text

    raw = "a_b@example.test"
    assert clean_text(raw, remove_extra_spaces=False, remove_urls_emails=True) == ""
    assert clean_character_document(make_document(raw), remove_extra_spaces=False, remove_urls_emails=True).page_content == ""
    historical_source = r"a\_b@example.test"
    assert clean_text(historical_source, remove_extra_spaces=False, remove_urls_emails=True) == "a\\"
    assert clean_character_document(make_document(historical_source), remove_extra_spaces=False, remove_urls_emails=True).page_content == "a\\"
```

最后两条刻意锁住历史 raw/character 分支的现有结果，不宣称它获得新 token 清洗算法。

- [ ] **Step 2（2–5 分钟）：增加 source 删除范围、附件和代码旁路断言。**

向同一文件追加：

```python
def test_email_cleaning_remaps_surviving_sources_and_attachment_exactly():
    from actweave_knowledge.extraction.contracts import AttachmentOccurrence, Document, SourceSpan

    email = escape_literal_text("a_b@example.test")
    ref = "a" * 64
    image = f"![image](knowledge-attachment:{ref})"
    left, right = "before ", " after "
    prefix = left + email + right
    image_span = SourceSpan(block_id="image", start=len(prefix), end=len(prefix) + len(image), location={"paragraph": 2})
    source = Document(
        page_content=prefix + image,
        source_spans=(
            SourceSpan(block_id="before", start=0, end=len(left), location={"paragraph": 1}),
            SourceSpan(block_id="email", start=len(left), end=len(left) + len(email), location={"paragraph": 1}),
            SourceSpan(block_id="after", start=len(left) + len(email), end=len(prefix), location={"paragraph": 1}),
            image_span,
        ),
        attachments=(AttachmentOccurrence(ref=ref, alt_text="image", source=image_span),),
    )
    (cleaned,) = clean_documents((source,), remove_extra_spaces=False, remove_urls_emails=True)
    assert cleaned.page_content == left + right + image
    assert [(span.block_id, cleaned.page_content[span.start:span.end]) for span in cleaned.source_spans] == [
        ("before", left), ("after", right), ("image", image),
    ]
    (occurrence,) = cleaned.attachments
    assert occurrence.source.start == len(left + right)
    assert cleaned.page_content[occurrence.source.start:occurrence.source.end] == image
    assert occurrence.source.location == {"paragraph": 2}


def test_email_cleaning_keeps_real_code_and_existing_link_label_rule():
    source = make_document(
        "`a_b@example.test`\n\n```text\n+alice@example.test\n```\n\n"
        "[label](https://example.invalid/path)\n\n"
        + escape_literal_text("https://example.invalid/a_b")
    )
    (cleaned,) = clean_documents((source,), remove_extra_spaces=False, remove_urls_emails=True)
    assert "`a_b@example.test`" in cleaned.page_content
    assert "```text\n+alice@example.test\n```" in cleaned.page_content
    assert "label" in cleaned.page_content
    assert "https://example.invalid" not in cleaned.page_content
```

- [ ] **Step 3（2–5 分钟）：先运行并定位为邮箱残余，不是字面量逃逸或环境问题。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_literal_cleaning.py -q
```

预期：escaped 地址清理和来源区间断言 FAIL；关闭开关、代码保护与 historical 分支可先 PASS。

- [ ] **Step 3b（2–5 分钟）：增加已启用空白规则对缩进实体的失败测试。**

向同一文件追加：

```python
@pytest.mark.parametrize("indent", ["    ", "\t  ", " \t", " \t   "])
def test_extra_space_cleaning_counts_generated_indentation_entities(indent):
    source = make_document(escape_literal_text(indent + "# text"))
    (cleaned,) = clean_documents((source,), remove_extra_spaces=True, remove_urls_emails=False)
    assert cleaned.page_content == " \\# text"
    assert "".join(cleaned.page_content[s.start:s.end] for s in cleaned.source_spans) == cleaned.page_content
    assert all(span.location == {"paragraph": 1} for span in cleaned.source_spans)


def test_space_cleaning_distinguishes_source_entity_text_and_preserves_code():
    source = make_document(escape_literal_text("&#32;  # literal"))
    (cleaned,) = clean_documents((source,), remove_extra_spaces=True, remove_urls_emails=False)
    assert cleaned.page_content == escape_literal_text("&#32; # literal")
    assert build_index_text(cleaned.page_content) == "&#32; # literal"
    code = make_document("`&#32;   x`\n\n```text\n&#9;   y\n```")
    assert clean_documents((code,), remove_extra_spaces=True, remove_urls_emails=False) == (code,)
    disabled = make_document(escape_literal_text("    # unchanged"))
    assert clean_documents((disabled,), remove_extra_spaces=False, remove_urls_emails=False) == (disabled,)
```

```bash
.venv/bin/python -m pytest tests/knowledge/test_literal_cleaning.py -k 'space_cleaning or extra_space' -q
```

预期：当前缩进实体与其它空白不能一起压缩，新增缩进参数用例 FAIL；字面实体与代码旁路可先 PASS。

- [ ] **Step 4（2–5 分钟）：增加仅用于 Markdown 源码的匹配式，复用现有 edit/remap。**

在 `_EMAIL` 定义后增加两条 token-only 表示匹配式：

```python
_MARKDOWN_EMAIL = re.compile(r"(?:[A-Za-z0-9]|\\?[_.+-])+@(?:[A-Za-z0-9]|\\?-)+\\?\.(?:[A-Za-z0-9]|\\?[.-])+")
_MARKDOWN_WHITESPACE_RUNS = re.compile(r"(?:[\t\f\v\x20\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]|(?<!\\)&#(?:32|9);){2,}")
```

仅在 `clean_documents` 的 `patterns` 构造中，将 `patterns.extend([(_EMAIL, ""), (_URL, "")])` 替换为：

```python
            patterns.extend([(_MARKDOWN_EMAIL, ""), (_URL, "")])
```

正则接受的可见邮箱字符仍为原来的 ASCII 集合，只允许句点、下划线、加号、短横前带一个 Markdown escape。保留 `_URL`、所有 `inline_atoms(text)` 默认参数、代码区域、link-label 处理及 `edit_document`，不做 unescape 全文再猜 offset。

同样只在 `clean_documents` 中，将空白规则的 `patterns.extend` 替换为：

```python
            patterns.extend([(_EXCESS_NEWLINES, "\n\n"), (_MARKDOWN_WHITESPACE_RUNS, " ")])
```

新式仅将 helper 生成的 `&#32;`/`&#9;` 视为一个水平空白；源文字面实体先经 `\&` 保护，负向后顾中的反斜杠检查防止误当空格。保留“至少两个水平空白才压缩”及换行规则，不增加其他实体解码或通用 HTML 处理。所有编辑仍在 Markdown 字符位置上交给现有 `edit_document`。

- [ ] **Step 5（2–5 分钟）：检查版本协同与 focused 回归后交付。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_literal_cleaning.py tests/knowledge/test_literal_markdown.py tests/knowledge/test_markdown_chunking.py -q
git diff --check
```

预期：PASS。此任务属于尚未发布的 v2 联合批次；在总计划 C1 已完成 `cleaner-v2` 与 token 入口版本检查之后共同验收，不能在旧身份下执行新清洗。不在本任务增写历史执行器，也不提交或发布。

## F5：解析身份、缓存、预览/摄取与联合分段门禁

**Files:**

- Test: `backend/tests/knowledge/test_parsing_profiles.py`
- Test: `backend/tests/knowledge/test_profile_admission.py`
- Test: `backend/tests/knowledge/test_extraction_cache.py`
- Test: `backend/tests/knowledge/test_parsing_pipeline.py`
- Test: `backend/tests/knowledge/test_literal_markdown.py`
- Read-only: `backend/packages/knowledge/actweave_knowledge/ingestion/profiles.py`、`extraction/manifest.py`、`storage/extractions.py`

**Interfaces:**

- Consumes: `canonical_parse_fingerprint(profile: ParseProfile) -> str`、`preview_fingerprint(*, source_sha256: str, extension: str, profile: ProcessingProfile, capability_revision: str) -> str`、`validate_frozen_processing_profile(value: dict | None, *, extension: str, registry: ExtractorRegistry) -> ProcessingProfile`；`prepared(h)`/`find(h, result, profile)`；`ingestion_harness` 的 `preview/upload/run_next_task/segments`。
- Produces: 没有新生产接口；增加真实边界测试。现有缓存/冻结检查已经满足的测试允许首次即通过，不为遵循 RED 人为改坏生产代码。
- `splitter-v3` 和直接 splitter 版本 guard 由分段计划负责；这里只验证 parse 与 chunk 的身份边界，不登记旧算法可执行能力。

- [ ] **Step 1（2–5 分钟）：增加 parse 与 preview 身份的独立测试。**

追加到 `test_parsing_profiles.py`：

```python
def test_adapter_and_chunk_versions_have_distinct_cache_and_preview_boundaries():
    from actweave_knowledge.extraction.manifest import canonical_parse_fingerprint
    from actweave_knowledge.ingestion.profiles import preview_fingerprint, validate_frozen_processing_profile

    profile = ProcessingProfile(parse=make_parse_profile(".txt"), chunk=make_chunk_profile())
    assert ":adapter-v2:" in profile.parse.extractor_version
    old_parse = profile.parse.model_copy(update={
        "extractor_version": profile.parse.extractor_version.replace(":adapter-v2:", ":adapter-v1:"),
    })
    old = profile.model_copy(update={"parse": old_parse})
    changed_chunk = profile.model_copy(update={
        "chunk": profile.chunk.model_copy(update={"splitter_version": "different-test-splitter"}),
    })
    assert canonical_parse_fingerprint(profile.parse) != canonical_parse_fingerprint(old.parse)
    assert canonical_parse_fingerprint(profile.parse) == canonical_parse_fingerprint(changed_chunk.parse)

    def fingerprint(value):
        return preview_fingerprint(source_sha256="a" * 64, extension=".txt", profile=value, capability_revision="same-test-capability")

    assert fingerprint(profile) != fingerprint(old)
    assert fingerprint(profile) != fingerprint(changed_chunk)
    with pytest.raises(ExtractionError) as error:
        validate_frozen_processing_profile(old.model_dump(mode="json"), extension=".txt", registry=default_registry())
    assert error.value.reason_code == "PROCESSING_PROFILE_UNAVAILABLE"
```

这里修改后的 `old_parse` 是“不可用身份”测试输入，不是假装真实历史完整 digest，更不登记历史执行器。

- [ ] **Step 1b（2–5 分钟）：证明旧 Adapter 预览不能进入新版本上传。**

追加到 `test_profile_admission.py`；复用该文件现有 `replace`、`hashlib`、profile 函数以及 `_harness/_seed_base/_upload/_table_counts`：

```python
@pytest.mark.asyncio
async def test_old_adapter_preview_rejected_before_rows_or_objects(postgres_database_url, tmp_path):
    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        upload = _upload(tmp_path)
        registry = default_registry()
        current = resolve_processing_profile(harness.service._settings, ProcessingParameters(), registry, extension=".txt")
        old_parse = current.parse.model_copy(update={
            "extractor_version": current.parse.extractor_version.replace(":adapter-v2:", ":adapter-v1:"),
        })
        assert old_parse != current.parse
        old = current.model_copy(update={"parse": old_parse})
        revision = build_file_capabilities(harness.service._settings, registry).capability_revision
        fingerprint = preview_fingerprint(
            source_sha256=hashlib.sha256(upload.source_path.read_bytes()).hexdigest(),
            extension=".txt", profile=old, capability_revision=revision,
        )
        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project, base, replace(upload, expected_preview_fingerprint=fingerprint))
        assert error.value.code == "KNOWLEDGE_CONFLICT"
        assert await _table_counts(harness) == (0, 0)
        assert harness.store.objects == {}
    finally:
        await harness.engine.dispose()
```

- [ ] **Step 2（2–5 分钟）：将相同边界落到真实 PostgreSQL extraction cache 查询。**

追加到 `test_extraction_cache.py`：

```python
@pytest.mark.asyncio
async def test_adapter_version_misses_ready_cache_without_deleting_current_result(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        reservation, result, profile = await prepared(h)
        stored = await h.store.complete(reservation, result)
        before = await quota_state(h)
        old = profile.model_copy(update={
            "extractor_version": profile.extractor_version.replace(":adapter-v2:", ":adapter-v1:"),
        })
        assert old != profile
        assert await find(h, result, old) is None
        assert await find(h, result, profile) == stored
        assert await quota_state(h) == before
        rows = await h.read_rows()
        assert len(rows["extractions"]) == 1
        assert rows["extractions"][0].state == "ready"
```

继续运行已有 `test_cache_identity_axes[chunk]`，证明 chunk 参数不属于提取缓存身份。不能为 cache miss 删除已发布旧 Extraction 或批量重建文档。

- [ ] **Step 3（2–5 分钟）：增加文字保真在冷解析、预览和失败后缓存重试中的一致性测试。**

追加到 `test_parsing_pipeline.py`：

```python
@pytest.mark.asyncio
async def test_literal_preview_cold_ingestion_and_cache_retry_are_identical(postgres_database_url, tmp_path):
    from actweave_knowledge.ingestion.index_text import build_index_text

    async with ingestion_harness(postgres_database_url) as harness:
        source = tmp_path / "literal.txt"
        raw = "---\n\n1. item &amp;\n\n    # literal"
        source.write_text(raw, encoding="utf-8")
        profile = ProcessingProfile(parse=make_parse_profile(".txt"), chunk=make_chunk_profile())
        preview = await harness.preview(source, profile)
        uploaded = await harness.upload(source, profile)
        harness.fake_model.fail = True
        await harness.run_next_task(expected_status="retry_wait")
        facts = await harness.resources.read_rows()
        doc = next(row for row in facts["documents"] if row.id == uploaded.id)
        assert doc.published_extraction_id is None
        assert len(facts["extractions"]) == 1 and facts["extractions"][0].state == "ready"
        extraction_id = facts["extractions"][0].id
        reads = sum(op == "get" and key == doc.storage_key for op, key in harness.resources.object_store.calls)

        harness.fake_model.fail = False
        await harness.run_next_task()
        rows = await harness.segments(uploaded.id)
        assert [row.content for row in rows] == [chunk.content for chunk in preview.chunks]
        assert [row.index_text for row in rows] == [build_index_text(chunk.content) for chunk in preview.chunks]
        assert [row.token_count for row in rows] == [chunk.token_count for chunk in preview.chunks]
        assert [row.source_spans for row in rows] == [
            [span.model_dump(mode="json") for span in chunk.source_spans] for chunk in preview.chunks
        ]
        assert all(row.extraction_id == extraction_id for row in rows)
        assert harness.fake_model.calls[-1] == [row.index_text for row in rows]
        joined = "\n".join(row.index_text for row in rows)
        assert all(value in joined for value in ("---", "1. item &amp;", "# literal"))
        assert not preview.warnings
        after = await harness.resources.read_rows()
        current = next(row for row in after["documents"] if row.id == uploaded.id)
        assert current.parse_warnings == []
        assert len(after["extractions"]) == 1
        assert reads == sum(op == "get" and key == doc.storage_key for op, key in harness.resources.object_store.calls)
```

这使用真实 PostgreSQL 和本地 fake object/model port，不能报告为真实 MinIO 或外部模型验证；图片闭包另由现有真实 Word fixture 流水线和 F2 共同覆盖。

- [ ] **Step 4（2–5 分钟）：新增与分段子计划共享的保护原子/双预算验收。**

追加到 `test_literal_markdown.py`：

```python
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
    for number in range(120):
        assert combined.count(f"item{number} ") == 1
    assert combined.count("&amp;") == 120
    assert combined.count("[x](url)") == 120
    assert combined.count(r"\path") == 120
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
```

若此测试发现 escape/entity 边界断裂，由分段子计划唯一拥有者修复既有原子处理；不能在这里删保护字符、放宽预算或并写 `structure.py`。此测试不让保护字符免计 Token。

- [ ] **Step 5（2–5 分钟）：运行离线和隔离数据库的新增验证。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_parsing_profiles.py::test_adapter_and_chunk_versions_have_distinct_cache_and_preview_boundaries tests/knowledge/test_literal_markdown.py -q
.venv/bin/python -m pytest tests/knowledge/test_literal_cleaning.py -q
.venv/bin/python -m pytest tests/knowledge/test_extraction_cache.py::test_adapter_version_misses_ready_cache_without_deleting_current_result tests/knowledge/test_extraction_cache.py::test_cache_identity_axes tests/knowledge/test_profile_admission.py::test_old_adapter_preview_rejected_before_rows_or_objects tests/knowledge/test_parsing_pipeline.py::test_literal_preview_cold_ingestion_and_cache_retry_are_identical -q
```

预期：F1–F4 与分段子计划完成后 PASS。PostgreSQL 未准备好时只能报告该项未验证；测试框架负责 clone `deerflow_test_*`，不得手工重置开发数据库或单独调用 `_install_full_schema()`。

- [ ] **Step 6（2–5 分钟）：执行现有冻结配置、图片流水线、失败原子性回归。**

```bash
.venv/bin/python -m pytest tests/knowledge/test_profile_admission.py tests/knowledge/test_parsing_pipeline.py tests/knowledge/test_extraction_cache.py tests/knowledge/test_parsing_governance.py -q
```

预期：PASS。特别保留旧配置拒绝且不写入、旧预览不被新 upload 接受、reparse 失败保持旧代次、失租约/版本竞争不发布、图片 inventory 不匹配失败的既有断言。重新向量化不重新切分的回归由总计划共同检查，不新增默认后台重解析。

- [ ] **Step 7（2–5 分钟）：在当前真实平台执行离线 OS 沙箱 matrix。**

```bash
format_matrix_dir=$(mktemp -d /tmp/actweave-format-v2.XXXXXX)
.venv/bin/python scripts/check_extraction_runtime.py --matrix --output "$format_matrix_dir/extraction-matrix.json"
```

预期：退出码 0，报告 `failed=0`；不可用路由必须 FAIL，不算 skip。保存工具返回的确切 `/tmp/actweave-format-v2.*` 路径给集成人员，不伪称 Linux 已验证、不将本机结果外推到目标部署。该命令仅临时文件与离线解析，不部署、不安装资源。

- [ ] **Step 8（2–5 分钟）：局部审查交付，由总计划统一格式化与整合。**

从仓库根运行：

```bash
git diff --check
git status --short
git diff --stat -- backend/packages/knowledge/actweave_knowledge/extraction backend/packages/knowledge/actweave_knowledge/ingestion/cleaner.py backend/tests/knowledge/test_literal_markdown.py backend/tests/knowledge/test_literal_cleaning.py backend/tests/knowledge/test_builtin_office_pdf.py backend/tests/knowledge/test_builtin_text_extractors.py backend/tests/knowledge/test_extraction_resources.py backend/tests/knowledge/test_parsing_profiles.py backend/tests/knowledge/test_profile_admission.py backend/tests/knowledge/test_extraction_cache.py backend/tests/knowledge/test_parsing_pipeline.py
```

总计划集成人员在确认 dirty diff 后唯一执行仓库要求的 `cd backend && make format` 和整体回归；本任务不能自行全仓格式化覆盖其他执行者未完成的业务文件。不新增 commit 步骤；提交仅在用户另行授权且确认精确文件后执行。

## 交给总计划的准确文案

由公共文档唯一拥有者合并到合适的稳定行为说明段落，不新增功能流水账：

> 非 Markdown 文件的普通原文按字面量序列化为显示 Markdown，保留原始符号的可见语义；HTML 原生结构和代码、Word 生成的链接/表格/图片，以及原生 Markdown/MDX 的既有结构处理不变。Word 自定义样式按段落及样式继承链最近的显式大纲级别识别一级至六级标题，不猜测自动编号、Title/Subtitle 或七至九级映射。

> Adapter 输出变更使用 `adapter-v2` 及经实际平台验证的资源锁。所有格式共用 Adapter 身份；已发布内容不自动重写，旧冻结配置不可执行时明确要求显式重新解析。重新向量化不重新提取或切分原文，不会自动获得文本保真修复。

> token 清洗使用 `cleaner-v2`，兼容显示 Markdown 中的邮箱转义和缩进实体；用户已有清洗开关、ASCII 邮箱范围及水平空白压缩规则不扩展。raw/character 清洗保持原算法，旧 token 配置不能在旧身份下执行新实现。

## 自审与交付清单

- [ ] A07/A08：F1/F2/F3 的符号、实体、缩进、跨 run、来源及图片闭包测试均有可执行代码。
- [ ] A09：F3 的真实 HTML/代码/pipe 测试和原有两种 Markdown adapter 回归同时通过。
- [ ] A2 清洗组合：F4C 覆盖 escaped 邮箱整体删除、缩进实体参与空白压缩、代码/源实体旁路、source/图片 remap、关闭开关和 historical raw/character 回归；依赖 C1 的 `cleaner-v2` guard。
- [ ] A10：F4 的直接级别、多级继承、最近显式值、unsupported、环、普通样式和内置行为均覆盖。
- [ ] A11/A12：F5 比较 preview、冷解析、缓存 retry、正式存储的 content/index/token/spans/warnings，缓存身份区分 parse/chunk。
- [ ] A13/A15/A17：保留旧 profile fail-closed、已发布数据、原子性、权限/租约与 reembed 边界；不声称有历史算法执行器。
- [ ] A18：不改用户现有 parser cache/PrefixTokenCounter 优化；联合分段测试消费当前真实实现。
- [ ] 只交付一个未发布的 `adapter-v2` 批次，revision 和已验证资源锁从 F1 开始一致；目标平台缺口如实记录。
- [ ] 汇报区分“新增测试的预期”“实际执行通过”“缺服务/平台未验证”，不将本文中的代码片段或内存探针称为实现已通过。
