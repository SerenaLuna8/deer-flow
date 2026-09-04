# RAG Chunking and Frozen-Version Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 token profile 的普通正文真正支持安全、有限的 overlap，并防止直接二次切分在旧版本身份下执行新算法。

**Architecture:** 在现有 `inline_atoms`、`_pack_group`、`slice_unit`、`join_units` 上扩展最小原子和正文后缀处理，不另建 splitter。`split_documents` 在 character 兼容分支之后验证 token splitter/cleaner 版本；图片、表格、代码和发布事务沿用既有路径。

**Tech Stack:** Python 3.12+、标准库、markdown-it-py 4.2.0、tiktoken 0.12.0、pytest/pytest-asyncio、现有 Knowledge 集成测试 harness。

**Spec:** `docs/superpowers/specs/2026-09-04-rag-quality-optimization-design.md`，重点第 4、5、9、10 节。总入口：`docs/superpowers/plans/2026-09-04-rag-quality-optimization.md`。

## Global Constraints

以下完整继承总入口的十项 Global Constraints，尤其逐字保留：

- 预览、摄取和显式重新解析继续共用现有 extraction 与 `split_documents`，不增加第二条默认处理路径。
- token profile 父段默认 1000、overlap 默认 100；父子模式子块默认 500。父段范围 200..4000、overlap 0..500 且小于父段、子块范围 100..2000 且小于父段，不变；历史 character 值不套用这一 Token 口径。
- token profile 的父段和 Child 分别满足显示 Markdown Token、`index_text` Token 及 16000 字符上限；标题、表头、分隔符和保护字符均参与相关预算。overlap 不是额外赠送的预算。
- 每文档父分段最多 5000，父子模式的累计 Child 向量条目另限 5000；两者不是合计 5000。沿用当前其余配额检查，不增加上限。
- Child 在各自父段内零重叠。父段重叠后，不同父段的 Children 可能覆盖相同原文；不新增跨父段全局去重。
- 冻结 character 算法不改写为 token 算法；重新向量化不读取原文件、不重新切分、不丢失人工编辑。
- 不跨页/标题/表格组，不复制图片，不增加列表内部 overlap，不以父块复制兜底缺失 Children。
- 新 token 版本为 `splitter-v3`；固定 Tokenizer 身份不变。格式序列化的邮箱/空白实体兼容需要 token cleaner 使用 `cleaner-v2`，由本计划统一修改版本常量；具体清洗接线、`adapter-v2` 与资源锁由格式子计划拥有。raw/character 清洗保持原实现。
- 无 schema、新依赖、检索融合修改。保留已有 PrefixTokenCounter、独立 parser 缓存及用户其他未提交修改。
- 所有步骤为未来实施任务，本轮只写计划。任务完成为审查交付点；无另行授权不提交、推送或部署。

---

## 文件结构与接口约定

所有路径相对仓库根目录。`K/` 表示 `backend/packages/knowledge/actweave_knowledge/`。

| 文件 | 本计划职责 |
| --- | --- |
| `K/ingestion/structure.py` | 原子边界：链接、行内代码、图片，以及 Markdown escape/实体；不改变表格组策略 |
| `K/ingestion/splitter.py` | token 版本拒绝、安全后缀、带 overlap 预算的后续正文重打包 |
| `K/ingestion/profiles.py` | `SPLITTER_VERSION="splitter-v3"`、`CLEANER_VERSION="cleaner-v2"`，不新增历史算法分派 |
| `backend/tests/knowledge/test_markdown_chunking.py` | 单元测试：普通正文、page、原子、预算、列表、来源、图片 |
| `backend/tests/knowledge/test_parsing_profiles.py` | splitter/preview/parse 指纹边界 |
| `backend/tests/knowledge/test_parsing_governance.py` | 旧 profile 手工派生拒绝，普通及 character 行为不变 |
| `backend/tests/knowledge/test_parsing_pipeline.py` | 新参数的预览/发布一致性 |

`K/segments/service.py` 是必须核对的调用者，正常情况下无需修改：它的 `_manual_derivation` 已在 Embedding 调用前执行，并将原冻结 ChunkProfile 传给 `split_documents`。预算错误或版本错误应沿现有异常路径返回。README 与指南由总计划单一集成人员更新。

## Task C1：版本拒绝与不可拆的字面量原子

**Files:**
- Modify: `K/ingestion/profiles.py:27`、`K/ingestion/splitter.py::split_documents`。
- Modify: `K/ingestion/structure.py::inline_atoms`。
- Test: `backend/tests/knowledge/test_markdown_chunking.py`、`test_parsing_profiles.py`。

**Interfaces:**
- Consumes: 既有 `ChunkProfile`、`ExtractionError`、`inline_atoms` 的代码/链接/图片识别。
- Produces: `inline_atoms(text: str, *, include_text_escapes: bool = False) -> list[tuple[int, int]]`；token splitter 拒绝非当前 splitter/cleaner 版本。切分方传 `include_text_escapes=True` 取得 escape/entity 区间，清洗方沿用默认值，不把可被 URL 规则删除的文字误当作受保护代码。格式子计划生成的 `&#32;`/`&#9;` 不会被字符 fallback 或 overlap 拆开。

- [ ] **Step 1：在 `test_markdown_chunking.py` 添加版本和原子红灯测试。** 复用该文件已有 imports，并增加 `StructureUnit, inline_atoms`：

```python
from actweave_knowledge.ingestion.structure import StructureUnit, inline_atoms


@pytest.mark.parametrize("field", ["splitter_version", "cleaner_version"])
def test_token_splitter_rejects_unavailable_version(field):
    profile = make_chunk_profile(**{field: "old-unavailable-build"})
    with pytest.raises(ExtractionError) as error:
        splitter.split_documents((make_document("alpha beta"),), profile=profile)
    assert error.value.reason_code == "PROCESSING_PROFILE_UNAVAILABLE"


@pytest.mark.parametrize("text,end", [(r"\# literal", 2), ("&#32;x", 5), ("&#9;x", 4), ("&amp;x", 5)])
def test_literal_escape_and_entity_are_atomic(text, end):
    assert (0, end) in inline_atoms(text, include_text_escapes=True)


def test_character_fallback_cannot_split_a_markdown_escape():
    with pytest.raises(ExtractionError) as error:
        list(splitter._split_ranges(StructureUnit(r"\#"), [""], lambda part: len(part.content) <= 1))
    assert error.value.reason_code == "ATOMIC_CONTENT_EXCEEDS_BUDGET"
```

- [ ] **Step 2：运行上述测试并确认失败原因。** 从 `backend/` 执行：

```bash
uv run pytest tests/knowledge/test_markdown_chunking.py -q -k 'unavailable_version or literal_escape_and_entity or cannot_split_a_markdown_escape'
```

预期当前失败：未知 splitter 被接受；`inline_atoms` 尚不接受新 keyword；反斜杠被切开。新增 keyword 是明确的接口红灯，不是错误拼写；若是其他 import/fixture 错误，先修正测试。

- [ ] **Step 3：在 `split_documents` 的 character 提前返回之后加入版本拒绝，并升级版本。** 使用函数内 import 避免模块初始化循环：

```python
# ingestion/profiles.py
SPLITTER_VERSION = "splitter-v3"
CLEANER_VERSION = "cleaner-v2"
```

```python
# ingestion/splitter.py, immediately after the character-profile return
from .profiles import CLEANER_VERSION, SPLITTER_VERSION

if profile.splitter_version != SPLITTER_VERSION or profile.cleaner_version != CLEANER_VERSION:
    raise ExtractionError(
        "PROCESSING_PROFILE_UNAVAILABLE",
        "原解析配置已不可用，请显式重新解析",
    )
```

不对 character 分支加入新的 token 版本限制，不调用完整 parser 可用性检查来阻断不需要重新解析的手工编辑。cleaner-v2 与格式子计划的表示兼容修复在同一次完整交付中生效；不能单独部署只有版本常量的中间状态。

- [ ] **Step 4：扩展 `inline_atoms` 的最前置识别。** 新增标准库 import 和有界实体表达式，并将函数签名改为 `def inline_atoms(text: str, *, include_text_escapes: bool = False) -> list[tuple[int, int]]:`：

```python
from html.entities import html5
from string import punctuation

_MARKDOWN_ENTITY = re.compile(
    r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});"
)
```

在 `while index < len(text):` 内、既有 backtick/link/image 检查之前插入：

```python
if include_text_escapes and text[index] == "\\" and index + 1 < len(text) and text[index + 1] in punctuation:
    ranges.append((index, index + 2))
    index += 2
    continue
if include_text_escapes and text[index] == "&":
    entity = _MARKDOWN_ENTITY.match(text, index)
    if entity and (entity[0].startswith("&#") or entity[0][1:] in html5):
        ranges.append((index, entity.end()))
        index = entity.end()
        continue
```

将 `splitter.py::_truncate_to_fit` 和 `_split_ranges` 中的调用改为 `inline_atoms(unit.content, include_text_escapes=True)`；C2 的三个新 helper 也使用这个参数。现有 cleaner、表格单元格分隔与图片识别用途保持默认调用。保留既有 backtick/link/image 控制流；不可把反斜杠跳过后仍将被保护的字符再次识别成结构起点。

在 `test_markdown_chunking.py` 增加一个清洗语义保护回归，不改其正则规则：

```python
def test_split_atom_protection_does_not_disable_plain_url_cleaning():
    from actweave_knowledge.ingestion.cleaner import clean_documents

    document = make_document(r"before https://example.test/a\_b after")
    [cleaned] = clean_documents((document,), remove_extra_spaces=False, remove_urls_emails=True)
    assert "https://" not in cleaned.page_content
    assert inline_atoms(r"\# plain") == []
    assert inline_atoms(r"\# plain", include_text_escapes=True) == [(0, 2)]
```

- [ ] **Step 5：添加 chunk 与 parse 指纹边界测试到 `test_parsing_profiles.py`。**

```python
def test_splitter_version_changes_preview_but_not_parse_cache_identity():
    from actweave_knowledge.extraction.manifest import canonical_parse_fingerprint
    from actweave_knowledge.ingestion.profiles import preview_fingerprint

    current = ProcessingProfile(parse=make_parse_profile(".md"), chunk=make_chunk_profile())
    old = current.model_copy(update={"chunk": current.chunk.model_copy(update={"splitter_version": "splitter-v2"})})
    assert current.chunk.splitter_version == "splitter-v3"
    assert canonical_parse_fingerprint(old.parse) == canonical_parse_fingerprint(current.parse)
    arguments = dict(source_sha256="a" * 64, extension=".md", capability_revision="same-capability")
    assert preview_fingerprint(profile=old, **arguments) != preview_fingerprint(profile=current, **arguments)
```

- [ ] **Step 6：运行局部绿灯和受影响回归。**

```bash
uv run pytest tests/knowledge/test_markdown_chunking.py tests/knowledge/test_parsing_profiles.py tests/knowledge/test_index_text.py tests/knowledge/test_knowledge_tokenizer.py -q
```

预期：所有适用断言通过；现有 helper 自动取当前版本，不把历史样例强行改成当前身份来掩盖拒绝行为。
- [ ] **Step 7：审查交付点。** C1 不是可单独部署的 splitter-v3 发布；C2/C3/C4 与总计划门禁完成后才可交付该版本的完整行为。

## Task C2：普通正文后缀与入站预算重分配

**Files:**
- Modify: `K/ingestion/structure.py::block_units`。
- Modify: `K/ingestion/splitter.py::_append_piece`、`_pack_group`，并在二者之间放置下列三个私有 helper。
- Test: `backend/tests/knowledge/test_markdown_chunking.py`。

**Interfaces:**
- Consumes: C1 的 `inline_atoms(..., include_text_escapes=True)`；既有 `StructureUnit`、`fits_chunk`、`slice_unit`、`join_units`、`_split_ranges`。
- Produces: `_overlap_suffix(unit, separators, fits) -> StructureUnit | None`；`_overlap_tail(pending, separator, overlap, separators) -> list[StructureUnit]`；`_reserve_overlap(prefix, retained, piece, separator, *, limit, overlap, separators, continuation) -> tuple[list[StructureUnit], StructureUnit, StructureUnit | None]`。下列代码给出完整类型定义。
- 保持 `split_documents` 的公开参数和返回类型不变；不新增用户配置。

- [ ] **Step 1：添加真实正文覆盖及进展红灯用例。** 追加到 `test_markdown_chunking.py`，复用已有 `split`、`assert_budgets`、`make_document`：

```python
@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("paragraph", " ".join(f"word{i}" for i in range(350))),
        ("paragraph", "\n".join(f"line{i} " + "plain " * 13 for i in range(40))),
        ("paragraph", "".join(chr(0x4E00 + i) for i in range(500))),
        ("page", "\n\n".join((f"para{i} " + "normal " * 45).strip() for i in range(5))),
        ("text", "\n\n".join((f"para{i} " + "normal " * 45).strip() for i in range(5))),
    ],
)
def test_body_overlap_consumes_new_source_without_inserting_separators(kind, text):
    document = make_document(text, location={"page": 1}).model_copy(update={"kind": kind})
    zero = split([document], size=200, overlap=0, child_size=100)
    drafts = split([document], size=200, overlap=60, child_size=100)
    assert [draft.content for draft in drafts] != [draft.content for draft in zero]
    assert_budgets(drafts)
    previous_start, previous_end, duplicated = -1, 0, False
    for draft in drafts:
        start = text.find(draft.content, previous_start + 1)
        assert start >= 0
        end = start + len(draft.content)
        assert end > previous_end
        assert not text[previous_end:start].strip()
        if start < previous_end:
            duplicated = True
            shared = text[start:previous_end]
            assert count_knowledge_tokens(shared) <= 60
            assert count_knowledge_tokens(splitter.build_index_text(shared)) <= 60
        assert all(span.role == "source" and span.location == {"page": 1} for span in draft.source_spans)
        previous_start, previous_end = start, end
    assert duplicated
    assert not text[previous_end:].strip()
```

- [ ] **Step 2：添加分隔符优先和列表保护红灯用例。** 补齐 `join_units` import：

```python
from actweave_knowledge.ingestion.structure import join_units


def test_overlap_suffix_uses_user_boundary_before_sentence_fallback():
    text = "前" * 200 + "分界" + "章" * 10 + "。" + "节" * 4
    unit = StructureUnit(text, (SourceSpan(block_id="body", start=0, end=len(text), location={"page": 2}),), kind="page")
    tail = splitter._overlap_tail([unit], "\n\n", 50, ["分界", "。", ""])
    assert len(tail) == 1
    assert tail[0].content == "章" * 10 + "。" + "节" * 4
    assert tail[0].source_spans[0].role == "source"
    assert tail[0].source_spans[0].location == {"page": 2}
    assert splitter.fits_chunk(tail[0].content, 50)


def test_overlap_keeps_whole_lists_but_does_not_slice_list_fragments():
    text = "- one\n- two"
    unit = StructureUnit(text, (SourceSpan(block_id="list", start=0, end=len(text), location={"paragraph": 1}),), kind="list")
    assert splitter._overlap_tail([unit], "\n\n", 50, ["\n", " ", ""]) == [unit]
    assert splitter._overlap_tail([unit], "\n\n", 1, ["\n", " ", ""]) == []
    fragment = splitter.replace(unit, kind="list_fragment")
    assert splitter._overlap_tail([fragment], "\n\n", 50, ["\n", " ", ""]) == []
```

- [ ] **Step 3：运行新增用例确认红灯。**

```bash
uv run pytest tests/knowledge/test_markdown_chunking.py -q -k 'body_overlap_consumes or overlap_suffix_uses or overlap_keeps_whole_lists'
```

预期：正文输出在 overlap 改变后仍相同；两个尚未定义的 helper 导致 AttributeError。C1 的版本与原子用例此时应保持通过。

- [ ] **Step 4：将普通 Markdown 段落块与来源容器类型分开。** 在 `block_units` 既有 `kind = {...}.get(...)` 之后、生成 `unit` 之前插入：

```python
if token.type == "paragraph_open" and document.kind not in {"table_header", "table_row", "fields"}:
    kind = "paragraph"
```

页/章/幻灯片等来源位置仍由原 Document spans 和 `structure_groups` 的边界拥有。不要只在 whitelist 添加 `page` 而漏掉 TXT 的 `text` 或其他普通正文容器，也不把 table header/row 改成普通段落。

- [ ] **Step 5：加入安全后缀选择 helper。** 它按分隔符优先级挑选经过双预算复核的后缀，不承诺 BPE 意义上的全局最长后缀：

```python
def _overlap_suffix(
    unit: StructureUnit,
    separators: list[str],
    fits: Callable[[StructureUnit], bool],
) -> StructureUnit | None:
    atoms = inline_atoms(unit.content, include_text_escapes=True)
    content_end = len(unit.content.rstrip())
    for separator in separators:
        starts = (
            [match.end() for match in re.finditer(re.escape(separator), unit.content)]
            if separator else list(range(1, len(unit.content)))
        )
        starts = [start for start in starts if start < content_end and not any(a < start < b for a, b in atoms)]
        low, high = 0, len(starts)
        while low < high:
            middle = (low + high) // 2
            if fits(slice_unit(unit, starts[middle], len(unit.content))):
                high = middle
            else:
                low = middle + 1
        if low < len(starts):
            candidate = slice_unit(unit, starts[low], len(unit.content))
            if fits(candidate) and _has_source_text(candidate):
                return candidate
    return None
```

候选只来自已生成的有界 pending 段（不超过 16000 字符），不对整篇百万字符原文建立字符级对象数组。

- [ ] **Step 6：加入完整单元优先的 tail helper。**

```python
def _overlap_tail(
    pending: list[StructureUnit],
    separator: str,
    overlap: int,
    separators: list[str],
) -> list[StructureUnit]:
    retained: list[StructureUnit] = []
    for unit in reversed(pending):
        if unit.kind not in {"paragraph", "markdown", "page", "text_fragment", "list"} or unit.attachments:
            break
        if any(unit.content.startswith("![", start) for start, _ in inline_atoms(unit.content, include_text_escapes=True)):
            break
        candidate = [unit, *retained]
        if fits_chunk(join_units(candidate, separator).content, overlap):
            retained = candidate
            continue
        if not retained and unit.kind != "list":
            suffix = _overlap_suffix(unit, separators, lambda value: fits_chunk(value.content, overlap))
            if suffix is not None:
                retained = [suffix]
        break
    return retained
```

- [ ] **Step 7：为后续满预算片段预留 overlap。** 新增：

```python
def _reserve_overlap(
    prefix: StructureUnit,
    retained: list[StructureUnit],
    piece: StructureUnit,
    separator: str,
    *,
    limit: int,
    overlap: int,
    separators: list[str],
    continuation: bool,
) -> tuple[list[StructureUnit], StructureUnit, StructureUnit | None]:
    def fits(value: StructureUnit) -> bool:
        return (
            fits_chunk(join_units(retained, separator).content, overlap)
            and fits_chunk(_render(prefix, _append_piece(retained, value, continuation=continuation), separator).content, limit)
        )

    if not retained or fits(piece):
        return retained, piece, None
    if piece.kind not in {"paragraph", "markdown", "page", "text_fragment"} or piece.attachments:
        while retained and not fits(piece):
            retained.pop(0)
        return retained, piece, None

    start = len(piece.content) - len(piece.content.lstrip())
    end = min(start + 1, len(piece.content))
    for atom_start, atom_end in inline_atoms(piece.content, include_text_escapes=True):
        if atom_start <= start < atom_end:
            end = atom_end
            break
    minimum = slice_unit(piece, 0, end)
    while retained and not fits(minimum):
        if len(retained) > 1 or retained[0].kind == "list":
            retained.pop(0)
            continue
        suffix = _overlap_suffix(
            retained[0],
            separators,
            lambda value: (
                fits_chunk(value.content, overlap)
                and fits_chunk(_render(prefix, _append_piece([value], minimum, continuation=continuation), separator).content, limit)
            ),
        )
        retained = [suffix] if suffix is not None else []
    if not retained or fits(piece):
        return retained, piece, None

    first = next(_split_ranges(piece, separators, fits))
    rest = slice_unit(piece, len(first.content), len(piece.content))
    return retained, replace(first, kind="text_fragment"), replace(rest, kind="text_fragment") if rest.content else None
```

只取得 `_split_ranges` 的首个结果，剩余原文按精确字符位置重新入队；不能提前用旧尾部预算拆完后面的长链接或图片。缩尾严格减少单元/字符，续分严格消费新正文。

- [ ] **Step 8：保留列表碎片身份和正文连续性。** 替换 `_append_piece`：

```python
def _append_piece(pending: list[StructureUnit] | tuple[StructureUnit, ...], piece: StructureUnit, *, continuation: bool) -> list[StructureUnit]:
    if continuation and pending:
        return [*pending[:-1], replace(join_units([pending[-1], piece], ""), kind=piece.kind)]
    return [*pending, piece]
```

替换 `_pack_group` 中现有 fragmented 转换与 continuation 判定：

```python
if fragmented and unit.kind not in {"code", "indented_code", "table_row", "fields"}:
    pieces = (replace(piece, kind="list_fragment" if unit.kind == "list" else "text_fragment") for piece in pieces)
```

```python
continuation = piece.kind in {"text_fragment", "list_fragment"} and piece_index > 0
```

同一函数 leading-image 普通文本分支的 `replacements` 改为：

```python
replacements = (
    (piece_index if index == 0 else 1, replace(part, kind="list_fragment" if unit.kind == "list" else "text_fragment"))
    for index, part in enumerate(subdivisions)
)
```

不保护该类型会让被拆开的长列表重新成为普通 text_fragment，从而错误获得列表内部重叠。

- [ ] **Step 9：接入三个 helper。** 在原 `if fresh: yield ...` 和 `prefix = context_unit(prefix)` 之后，替换原 `retained = []` 到 `candidate = [*pending, piece]` 的整个 overlap 分支：

```python
retained = []
if overlap and piece.kind in {"paragraph", "markdown", "page", "text_fragment", "list"} and not piece.attachments:
    retained = _overlap_tail(pending, separator, overlap, separators)
pending, piece, rest = _reserve_overlap(
    prefix, retained, piece, separator,
    limit=limit, overlap=overlap, separators=separators, continuation=continuation,
)
if rest is not None:
    piece_stream = chain(((1, rest),), piece_stream)
fresh = False
candidate = _append_piece(pending, piece, continuation=continuation)
```

循环末尾原 `pending = candidate; fresh = True` 改为：

```python
pending = candidate
fresh = fresh or _has_source_text(piece)
```

图片保护两个特殊分支及 prefix 降级分支原样保留；不要将整个函数换成新框架。

- [ ] **Step 10：运行 C2 已新增测试，再跑全文件。**

```bash
uv run pytest tests/knowledge/test_markdown_chunking.py -q -k 'body_overlap_consumes or overlap_suffix_uses or overlap_keeps_whole_lists'
uv run pytest tests/knowledge/test_markdown_chunking.py -q
```

预期：新增行为和既有表格、图片、代码、character、预算测试全部通过。
- [ ] **Step 11：审查交付点。** 输出 source/text 差异，确认未改变 CSV/Excel 逐行组策略、未增加列表/图片重叠，也未丢失已有缓存计数优化。

## Task C3：极限预算与跨包原子回归

**Files:**
- Test: `backend/tests/knowledge/test_markdown_chunking.py`。
- Read: C1/C2 实现和格式子计划的 escape/entity 输出。
- Modify: 仅在新增回归暴露问题时修正 C1/C2 所属函数，不修改配额或放宽测试。

**Interfaces:**
- Consumes: C1/C2 的具体 helper 和现有 `assert_budgets`。
- Produces: 链接、escape/entity、接近满额 overlap 的可运行回归；这些是新增验证，不为制造红灯而回退用户代码。

- [ ] **Step 1：添加原子大于可用余量的回归。**

```python
def test_full_incoming_atomic_prefix_shrinks_tail_without_splitting_link():
    old_text = "旧" * 30
    old = StructureUnit(old_text, (SourceSpan(block_id="old", start=0, end=len(old_text), location={"paragraph": 1}),), kind="text_fragment")
    link = "[link](https://example.test/" + "x/" * 150 + ")"
    text = link + "新" * 100
    incoming = StructureUnit(text, (SourceSpan(block_id="new", start=0, end=len(text), location={"paragraph": 2}),), kind="text_fragment")
    assert splitter.fits_chunk(link, 200)
    assert not splitter.fits_chunk(old_text + link, 200)
    retained, first, rest = splitter._reserve_overlap(
        StructureUnit(""), [old], incoming, "\n\n",
        limit=200, overlap=60, separators=["\n\n", " ", ""], continuation=True,
    )
    assert retained and len(retained[0].content) < len(old_text)
    assert first.content.startswith(link)
    assert rest is not None
    assert first.content + rest.content == text
    assert splitter.fits_chunk(join_units(retained, "\n\n").content, 60)
    packed = splitter._append_piece(retained, first, continuation=True)
    assert splitter.fits_chunk(join_units(packed, "\n\n").content, 200)
    assert all(span.role == "source" for value in [*retained, first, rest] for span in value.source_spans)
    assert inline_atoms(first.content, include_text_escapes=True)[0] == (0, len(link))
```

- [ ] **Step 2：添加极大 overlap 和 escape/entity 切分边界回归。**

```python
def test_positive_overlap_with_nearly_full_budget_still_terminates():
    text = "".join(chr(0x5000 + i) for i in range(220))
    drafts = split([make_document(text)], size=200, overlap=190, child_size=100)
    assert 1 < len(drafts) <= len(text)
    assert_budgets(drafts)
    previous_start, previous_end, duplicated = -1, 0, False
    for draft in drafts:
        start = text.find(draft.content, previous_start + 1)
        assert start >= 0
        end = start + len(draft.content)
        assert end > previous_end
        duplicated = duplicated or start < previous_end
        previous_start, previous_end = start, end
    assert previous_end == len(text)
    assert duplicated


@pytest.mark.parametrize("encoded", [r"\#", "&#32;", "&#9;"])
def test_overlap_never_cuts_literal_serialization_atoms(encoded):
    text = "".join(chr(0x4E00 + index) + encoded for index in range(180))
    atoms = inline_atoms(text, include_text_escapes=True)
    drafts = split([make_document(text)], size=200, overlap=60, child_size=100)
    assert_budgets(drafts)
    previous_start, previous_end = -1, 0
    for draft in drafts:
        start = text.find(draft.content, previous_start + 1)
        assert start >= 0
        end = start + len(draft.content)
        assert not any(a < start < b or a < end < b for a, b in atoms)
        assert end > previous_end
        previous_start, previous_end = start, end
    assert previous_end == len(text)
```

- [ ] **Step 3：运行 C3 和既有边界回归。**

```bash
uv run pytest tests/knowledge/test_markdown_chunking.py -q -k 'full_incoming_atomic or positive_overlap_with_nearly or never_cuts_literal or pdf_pages or table or image or code or hard_limit or character'
```

预期：新增用例通过。若失败，先记录最小样例及哪一层预算/来源断言失败，再修正 C1/C2；不增加例外白名单或关闭预算。
- [ ] **Step 4：审查交付点。** 确认 paragraph/word/image 等既有边界用例未被删除，新增原子参数没有传播到 cleaner 的默认保护语义。

## Task C4：手工派生零发送、零写入及预览发布一致

**Files:**
- Test: `backend/tests/knowledge/test_parsing_governance.py`。
- Test: `backend/tests/knowledge/test_parsing_pipeline.py`。
- Read: `K/segments/service.py::_manual_derivation`、`_embed_for_document`；`K/ingestion/pipeline.py`；`K/ingestion/reembed.py`。
- Modify: 原则上没有新增实现；版本拒绝由 C1 共用入口负责。如果检查表明实际调用顺序发生改变，先按当前流程更新测试锚点，不重复增加另一套版本规则。

**Interfaces:**
- Consumes: C1 的版本拒绝；已有 `FakeModelClient`、`ingestion_harness`、`KnowledgeSegmentCreate/Update`。
- Produces: 对真实派生调用顺序与 PostgreSQL 发布边界的证明；假对象存储/假模型不代表真实外部服务。

- [ ] **Step 1：增加无需数据库的零发送和兼容分支测试。** 在 `test_parsing_governance.py` 追加：

```python
from types import SimpleNamespace
from ingestion_test_helpers import FakeModelClient
from actweave_knowledge.extraction.contracts import ExtractionError
from actweave_knowledge.segments.service import KnowledgeSegmentService, _manual_derivation


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["splitter_version", "cleaner_version"])
async def test_manual_old_token_profile_rejects_before_model_dispatch(field):
    chunk = make_chunk_profile(mode="parent_child", size=200, child_size=100, overlap=0)
    chunk = chunk.model_copy(update={field: "old-unavailable-build"})
    profile = ProcessingProfile(parse=make_parse_profile(".md"), chunk=chunk)
    document = SimpleNamespace(
        chunking_mode="parent_child", child_chunk_size=100, child_chunk_separator="\n\n",
        parsing_profile=profile.model_dump(mode="json"),
    )
    client = FakeModelClient()
    service = SimpleNamespace(_client=client)
    with pytest.raises(ExtractionError) as error:
        await KnowledgeSegmentService._embed_for_document(
            service, document, "alpha beta gamma", None,
            available_vector_entries=5000, authority=None,
        )
    assert error.value.reason_code == "PROCESSING_PROFILE_UNAVAILABLE"
    assert client.calls == []


@pytest.mark.parametrize("mode,unit", [("general", "token"), ("parent_child", "character"), ("parent_child", None)])
def test_manual_non_resplitting_and_character_paths_keep_their_contract(mode, unit):
    profile = None if unit is None else ProcessingProfile(
        parse=make_parse_profile(".md"),
        chunk=make_chunk_profile(unit=unit, mode=mode, size=200, child_size=100, overlap=0, splitter_version="historical-version"),
    ).model_dump(mode="json")
    document = SimpleNamespace(
        chunking_mode=mode, child_chunk_size=100, child_chunk_separator="\n\n", parsing_profile=profile,
    )
    content = "alpha beta gamma"
    index_text, token_count, children = _manual_derivation(document, content)
    assert index_text == content
    assert token_count == count_knowledge_tokens(content)
    assert bool(children) == (mode == "parent_child")
    assert all(child.source_spans == () for child in children)
```

这是在不创建服务基础设施的情况下调用真实 `_embed_for_document`；`None` material 不会被使用，因为预期版本拒绝必须先发生。规划阶段已用当前实现验证相反事实：旧 profile 当前确实可到达 FakeModelClient，因此这不是不可触发的用例。

- [ ] **Step 2：执行纯函数/假模型测试。**

```bash
uv run pytest tests/knowledge/test_parsing_governance.py -q -k 'manual_old_token_profile or manual_non_resplitting'
```

预期：旧 token 双版本拒绝、零模型请求；三个不需要新 token 重切的分支仍通过。不能为了通过测试给普通/character 编辑增加 parser 可用性依赖。

- [ ] **Step 3：添加 PostgreSQL 的新增/编辑失败不发布测试。** 复用文件已存在的 imports 和 harness；补齐 `KnowledgeDocumentRow`：

```python
from actweave_knowledge.persistence.models import KnowledgeDocumentRow


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_old_profile_manual_mutations_leave_publication_unchanged(postgres_database_url, tmp_path, operation):
    source = tmp_path / "parent.md"
    source.write_text("原始正文。" * 10, encoding="utf-8")
    profile = ProcessingProfile(parse=make_parse_profile(".md"), chunk=make_chunk_profile(mode="parent_child", size=200, child_size=100, overlap=0))
    async with ingestion_harness(postgres_database_url) as harness:
        document = await harness.upload(source, profile)
        await harness.run_next_task()
        [parent] = await harness.segments(document.id)
        before = (parent.id, parent.content, parent.index_text, parent.document_version, parent.source_spans)
        async with harness.resources.session_factory() as session, session.begin():
            row = await session.get(KnowledgeDocumentRow, document.id)
            frozen = {**row.parsing_profile, "chunk": {**row.parsing_profile["chunk"], "splitter_version": "splitter-v2"}}
            row.parsing_profile = frozen
            before_children = list((await session.execute(
                select(KnowledgeSegmentChildRow.id, KnowledgeSegmentChildRow.content, KnowledgeSegmentChildRow.index_text)
                .where(KnowledgeSegmentChildRow.knowledge_segment_id == parent.id)
                .order_by(KnowledgeSegmentChildRow.position)
            )).all())
        harness.fake_model.calls.clear()
        with pytest.raises(KnowledgeError) as error:
            if operation == "update":
                await harness.module.update_segment(harness.resources.project_id, parent.id, KnowledgeSegmentUpdate(content="修改后的正文"), authority=harness.authority)
            else:
                await harness.module.create_segment(harness.resources.project_id, document.id, KnowledgeSegmentCreate(content="新增正文"), authority=harness.authority)
        assert error.value.reason_code == "PROCESSING_PROFILE_UNAVAILABLE"
        assert harness.fake_model.calls == []
        [after] = await harness.segments(document.id)
        assert (after.id, after.content, after.index_text, after.document_version, after.source_spans) == before
        async with harness.resources.session_factory() as session:
            row = await session.get(KnowledgeDocumentRow, document.id)
            assert row.parsing_profile == frozen
            after_children = list((await session.execute(
                select(KnowledgeSegmentChildRow.id, KnowledgeSegmentChildRow.content, KnowledgeSegmentChildRow.index_text)
                .where(KnowledgeSegmentChildRow.knowledge_segment_id == parent.id)
                .order_by(KnowledgeSegmentChildRow.position)
            )).all())
        assert after_children == before_children
```

只在框架创建的隔离测试库内构造旧 profile，不把该赋值作为真实文档迁移或手工修表手段。

- [ ] **Step 4：增加普通长段落的预览/摄取一致性测试。** 在 `test_parsing_pipeline.py` 追加，`.md` 避免把格式行为混入本包的 overlap 测试：

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["general", "parent_child"])
async def test_long_prose_overlap_preview_matches_published_derivations(postgres_database_url, tmp_path, mode):
    source = tmp_path / "overlap.md"
    source.write_text(" ".join(f"word{index}" for index in range(350)), encoding="utf-8")
    profile = ProcessingProfile(parse=make_parse_profile(".md"), chunk=make_chunk_profile(mode=mode, size=200, overlap=60, child_size=100))
    async with ingestion_harness(postgres_database_url) as harness:
        preview = await harness.preview(source, profile)
        uploaded = await harness.upload(source, profile)
        await harness.run_next_task()
        rows = await harness.segments(uploaded.id)
        assert [row.content for row in rows[:10]] == [chunk.content for chunk in preview.chunks]
        assert [row.source_spans for row in rows[:10]] == [[span.model_dump(mode="json") for span in chunk.source_spans] for chunk in preview.chunks]
        assert [row.token_count for row in rows[:10]] == [chunk.token_count for chunk in preview.chunks]
        assert all(row.index_text and row.token_count <= 200 for row in rows)
        if mode == "general":
            assert harness.fake_model.calls[-1] == [row.index_text for row in rows]
        else:
            from actweave_knowledge.persistence.models import KnowledgeSegmentChildRow
            async with harness.resources.session_factory() as session:
                children = list((await session.scalars(
                    select(KnowledgeSegmentChildRow)
                    .join(KnowledgeSegmentRow, KnowledgeSegmentRow.id == KnowledgeSegmentChildRow.knowledge_segment_id)
                    .where(KnowledgeSegmentRow.knowledge_document_id == uploaded.id)
                    .order_by(KnowledgeSegmentRow.position, KnowledgeSegmentChildRow.position)
                )).all())
            assert harness.fake_model.calls[-1] == [child.index_text for child in children]
            assert all(row.embedding is None for row in rows)
            assert all(child.token_count <= 100 for child in children)
```

- [ ] **Step 5：执行集成与既有生命周期回归。** 已确认仓库根 `.env` 为开发环境后，从 `backend/` 执行：

```bash
uv run --env-file ../.env pytest tests/knowledge/test_parsing_governance.py tests/knowledge/test_parsing_pipeline.py tests/knowledge/test_profile_admission.py tests/knowledge/test_reembedding.py -q
```

预期：实际 PostgreSQL 隔离测试通过，未知版本不派发/不发布；重解析与重新向量化的现有权限、缓存、摘要语义不退化。测试 harness 的对象存储与模型是假的，真实服务门仍由总计划负责。
- [ ] **Step 6：审查交付点。** 对照 Spec A01–A06、A11–A15、A18，逐项记录测试节点与输出。所有实际代码改动由总计划最终格式化、全局回归和审查后交付；不在这里自动提交或部署。
