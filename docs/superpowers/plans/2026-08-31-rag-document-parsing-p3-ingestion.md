# P3 Token 切分与摄取接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让正式摄取、预览、人工分段与重处理使用同一份可追踪 Markdown/Token 派生结果。

**Architecture:** 消费 P1 ExtractionResult 和 P2 StoredExtraction；第二阶段只做清洗、结构保护、Token 切分与 index_text 派生。既有 Worker 控制租约和原子发布，Gateway 控制预览与用户操作权限；检索返回展示内容，模型评分使用 index_text。

**Tech Stack:** Python 3.12、tiktoken 0.12.0、markdown-it-py 4.2.0、SQLAlchemy、pgvector、pytest。

**Spec:** [设计规格](../specs/2026-08-31-rag-document-parsing-design.md)；[总计划与共用 Interface](2026-08-31-rag-document-parsing.md)；前置 [P1](2026-08-31-rag-document-parsing-p1-extraction.md)、[P2](2026-08-31-rag-document-parsing-p2-storage.md)。

## Global Constraints

- 继承总计划全部约束；同名类型只能由总计划指定的文件拥有。
- 父段 1000 Token、overlap 100 Token、子块 500 Token、子块零重叠；父段仍受 4000 字符硬上限约束。
- Tokenizer Profile ID `knowledge-cl100k-v1`；不是目标模型计费 Token；缺资源明确失败，禁止下载降级。
- 原文件 ≤50 MiB、正文 ≤5,000,000 字符、父段与向量条目分别 ≤5,000；图片与缓存预算沿用 P2。
- 预览前 10 父段、最多 20 张缩略图、每张 ≤128 KiB、合计 ≤2 MiB，不能写 DB/MinIO/Task。
- 提取缓存键不包含 Tokenizer、chunk_size、清洗或 separator；预览 fingerprint 必须包含这些及原文件摘要。
- 本包不新增 Provider HTTP 客户端，不把 OCR 混进 Token/文本处理；当前规格不包含 OCR。
- P2 一次性拥有本期 schema 更新；P3 消费 parsing_profile/index_text/token_count/source_spans/附件关系，不能再私改第二份 schema。
- 下面命令在实施时运行；本文生成时没有运行实现测试。所有提交步骤仅在当时用户已授权时执行，并仅暂存任务文件。

## P3-T1：固定本地 Tokenizer 与确定性 index_text

**Files**

- Create: `backend/packages/knowledge/actweave_knowledge/ingestion/tokenizer.py`、`index_text.py`、`tokenizer_data/manifest.json`、`tokenizer_data/cl100k_base.tiktoken`。
- Create: `backend/scripts/prepare_knowledge_tokenizer.py`。
- Modify: `backend/packages/knowledge/pyproject.toml`、`backend/uv.lock`、`backend/Dockerfile`。
- Test: `backend/tests/knowledge/test_knowledge_tokenizer.py`、`test_index_text.py`；Modify: `backend/tests/knowledge/parsing_test_helpers.py`（Token profile使用真实摘要）。

**Interfaces**

- Consumes: P1 的规范化 Markdown；总计划 `ChunkProfile`。
- Produces: `count_knowledge_tokens(text, *, profile_id='knowledge-cl100k-v1') -> int`；`build_index_text(markdown: str) -> str`；`tokenizer_fingerprint() -> str`。三者不能读网络或宿主配置。

- [ ] **1. 写两个失败测试。**

```python
from actweave_knowledge.ingestion.tokenizer import count_knowledge_tokens
from actweave_knowledge.ingestion.index_text import build_index_text

def test_cl100k_count_uses_tokens_not_characters():
    assert count_knowledge_tokens('hello world') == 2
    assert count_knowledge_tokens('hello world') != len('hello world')

def test_index_text_keeps_code_and_labels_but_not_attachment_uri():
    text = '# 设备\n\n- 管理 IP：10.0.0.1\n\n```cpp\nList<int> values;\n```\n\n![端口照片](knowledge-attachment:' + 'a' * 64 + ')'
    indexed = build_index_text(text)
    assert '管理 IP' in indexed and '10.0.0.1' in indexed
    assert 'List<int> values;' in indexed
    assert 'knowledge-attachment:' not in indexed and 'a' * 64 not in indexed
```

- [ ] **2. 运行 red。** 工作目录 `backend/`：

```bash
env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_knowledge_tokenizer.py tests/knowledge/test_index_text.py -q
```

预期新模块缺失或实际断言失败；依赖安装错误不能冒充功能 red。

- [ ] **3. 构建时导出词表，运行时只读取本地资源。** 当前 lock 已有 tiktoken 0.12.0 和 markdown-it-py 4.2.0，给 knowledge 包声明直接依赖，不盲目降级其它包。准备脚本可在显式安装/镜像构建阶段读取 tiktoken 的 cl100k 配置，导出 base64-token/rank 文本、pat_str、special_tokens、文件 SHA-256；运行时不调用 `get_encoding()` 触发隐式下载。

准备脚本的核心输出逻辑：

```python
import base64
import hashlib
import json
from pathlib import Path
from tiktoken_ext.openai_public import cl100k_base

def export_tokenizer(output: Path) -> None:
    config = cl100k_base()  # 仅显式构建命令允许准备上游数据。
    output.mkdir(parents=True, exist_ok=True)
    rows = sorted(config['mergeable_ranks'].items(), key=lambda item: item[1])
    payload = b''.join(base64.b64encode(token) + b' ' + str(rank).encode('ascii') + b'\n' for token, rank in rows)
    (output / 'cl100k_base.tiktoken').write_bytes(payload)
    manifest = {'profile_id': 'knowledge-cl100k-v1', 'sha256': hashlib.sha256(payload).hexdigest(),
                'pat_str': config['pat_str'], 'special_tokens': config['special_tokens']}
    (output / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False), encoding='utf-8')
```

给脚本增加标准 argparse `--output`，只接受操作者指定的本地输出目录，不从服务启动调用。将结果纳入包资源和镜像，不依赖用户缓存目录。

运行时核心：

```python
import hashlib
import json
from functools import lru_cache
from importlib.resources import files
import tiktoken
from tiktoken.load import load_tiktoken_bpe

@lru_cache(maxsize=1)
def _encoder() -> tiktoken.Encoding:
    root = files('actweave_knowledge.ingestion').joinpath('tokenizer_data')
    manifest = json.loads(root.joinpath('manifest.json').read_text(encoding='utf-8'))
    data = root.joinpath('cl100k_base.tiktoken')
    payload = data.read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest['sha256']:
        raise ValueError('TOKENIZER_UNAVAILABLE')
    return tiktoken.Encoding(name='knowledge-cl100k-v1', pat_str=manifest['pat_str'],
                            mergeable_ranks=load_tiktoken_bpe(str(data), expected_hash=manifest['sha256']),
                            special_tokens=manifest['special_tokens'])

def count_knowledge_tokens(text: str, *, profile_id: str = 'knowledge-cl100k-v1') -> int:
    if profile_id != 'knowledge-cl100k-v1':
        raise ValueError('TOKENIZER_UNAVAILABLE')
    return len(_encoder().encode_ordinary(text))
```

将文件缺失、JSON损坏、hash错映射为现有 KnowledgeError 的安全 reason_code，不暴露路径。新增 `tokenizer_fingerprint() -> str` 返回规范 manifest 的 SHA-256，单独进入 ChunkProfile，不进入 ParseProfile；从本任务起，共用 `make_chunk_profile` 的 token 模式默认使用该真实摘要，character 模式的Tokenizer字段为null。

- [ ] **4. 用 Markdown token tree 生成 index_text。** 使用 `MarkdownIt('commonmark', {'html': False}).enable('table')`；保留 inline 的 text/code_inline、fence/code_block 内容、图像非空 alt、软/硬换行；link_open/image 的 href/src 不进入结果。按块用换行连接并规范空行；不要对正文运行 `<.*?>` 删除。为表格、链接文本、字面 `<IP>`、恶意 HTML 字面量补充测试。

在同模块新增 `has_indexable_source_text(documents: tuple[Document,...]) -> bool`：按原始source span覆盖的节点判定真实非空文字，排除image节点的alt、context_prefix及生成的失败占位。它只决定文档是否可进入文本索引，不替代index_text。P3-T4/T5在分段和调用Embedding前使用它；纯图片即使有“本页图片”alt也必须返回 `NO_INDEXABLE_TEXT`。

```python
from actweave_knowledge.extraction.contracts import Document, SourceSpan
from actweave_knowledge.ingestion.index_text import has_indexable_source_text

def test_generated_image_alt_cannot_turn_scan_into_searchable_text():
    markdown = '![本页图片](knowledge-attachment:' + 'a' * 64 + ')'
    image_only = Document(page_content=markdown, source_spans=(
        SourceSpan(block_id='page:1:image:1', start=0, end=len(markdown), location={'page': 1}),))
    assert not has_indexable_source_text((image_only,))
```
- [ ] **5. 跑 green 和资源故障测试。** 新进程、空用户缓存且阻断网络连接时可 count；删资源或改一字节时必须失败，不下载。只有显式 prepare 脚本在构建阶段可联网。

```bash
PYTHONPATH=. uv run python scripts/prepare_knowledge_tokenizer.py --output packages/knowledge/actweave_knowledge/ingestion/tokenizer_data
env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_knowledge_tokenizer.py tests/knowledge/test_index_text.py -q
```

- [ ] **6. 检查并交付本任务。** 覆盖 A10/A11/A24；检查资源 manifest 无本机路径，记录词表摘要和 lock。授权提交时仅暂存本任务 Files。

## P3-T2：结构保护、来源映射与父子 Token 分段

**Files**

- Modify: `backend/packages/knowledge/actweave_knowledge/ingestion/splitter.py`、`cleaner.py`、`__init__.py`。
- Create: `backend/packages/knowledge/actweave_knowledge/ingestion/structure.py`、`source_mapping.py`。
- Test: `backend/tests/knowledge/test_markdown_chunking.py`；扩展 `test_ingestion.py` 中旧字符切分门。

**Interfaces**

- Consumes: `Document`、`ChunkProfile`、P3-T1 的计量与 index_text。
- Produces: 总计划定义的 `SegmentDraft`、`ChildDraft` 和 `split_documents`。内部 `StructureUnit(content, source_spans, heading_path, kind, attachments)` 只在 structure.py 使用，不扩大公开 DTO。

- [ ] **1. 先写结构回归测试。**

```python
from actweave_knowledge.ingestion.splitter import split_documents
from actweave_knowledge.ingestion.tokenizer import count_knowledge_tokens
from parsing_test_helpers import make_chunk_profile, make_document

def test_table_continuations_keep_header_and_source():
    rows = ['| 编号 | 处置 |', '| --- | --- |'] + [f'| E{i:03d} | 检查邻居并核对接口状态。' + '确认链路。' * 30 + ' |' for i in range(20)]
    source = make_document('\n'.join(rows), location={'sheet': '故障', 'row': 1})
    drafts = split_documents((source,), profile=make_chunk_profile(size=200, overlap=0, child_size=100))
    assert len(drafts) > 1
    for draft in drafts:
        assert '编号' in draft.content and '处置' in draft.content
        assert count_knowledge_tokens(draft.content) <= 200
        assert count_knowledge_tokens(draft.index_text) <= 200
        assert len(draft.content) <= 4000 and draft.source_spans

def test_long_code_preserves_generics_and_balances_fences():
    source = make_document('```cpp\n' + 'List<int> values;\n' * 300 + '```', location={'paragraph': 2})
    drafts = split_documents((source,), profile=make_chunk_profile(size=200, overlap=0, child_size=100))
    assert len(drafts) > 1
    assert all(d.content.startswith('```cpp\n') and d.content.rstrip().endswith('```') for d in drafts)
    assert sum(d.content.count('List<int> values;') for d in drafts) == 300
```

- [ ] **2. 跑 red。**

```bash
env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_markdown_chunking.py -q
```

- [ ] **3. 实现结构单元和偏移映射。** Markdown parser 的 token.map 转成原规范字符串的字符 offset；从 P1 SourceSpan 取交集，保留真实来源，不附整章位置。新增映射函数及测试：

```python
from actweave_knowledge.extraction.contracts import SourceSpan

def clip_source_spans(spans: tuple[SourceSpan, ...], start: int, end: int) -> tuple[SourceSpan, ...]:
    result = []
    for span in spans:
        left, right = max(start, span.start), min(end, span.end)
        if left < right:
            result.append(span.model_copy(update={'start': left - start, 'end': right - start}))
    return tuple(result)
```

测试三个段落合并后跨第二段切开，左段只含第一/第二来源，右段只含第二/第三。插入标题/表头时单独加 `role='context_prefix'`，指向原标题来源并修正后续偏移。

- [ ] **4. 实现双预算打包。** 每次追加结构单元先构造“标题前缀+正文+必要闭合语法”，同时检查展示文字 Token、index_text Token 和字符数。超限则 flush 当前段；单单元超限按表格行→字段、代码行→Unicode边界、普通段落→用户separator→fallback逐级拆；图片ref不可拆。图片纯ref且没有有效 index_text 不生成可索引段。

预算判定的共享函数：

```python
def fits_chunk(markdown: str, token_limit: int) -> bool:
    return (len(markdown) <= 4000
            and count_knowledge_tokens(markdown) <= token_limit
            and count_knowledge_tokens(build_index_text(markdown)) <= token_limit)
```

如果必要的标题/表头前缀本身已超过预算，返回明确的参数/资源错误及安全原因 `CONTEXT_PREFIX_EXCEEDS_BUDGET`，提示增大分段预算或调整来源内容；不能截断列名、标题路径后伪装为完整结果。增加该样例验证有限步骤内终止、无部分发布。

- [ ] **5. 实现重叠和 children。** 普通文本完整单位的后缀最多 overlap Token；表格行/PDF页间不跨界重叠；防止只含carry-over的新段。子块在父正文内部使用 child_separator 与子Token预算，零重叠，index_text非空；parent_child父段附图，child不重复独立存附件。
- [ ] **6. 保留清洗及旧 profile。** `character` 继续走旧算法；新 `token` 不重解释旧值。URL/email清洗仅作用于可修改的普通文本节点，保留代码及内部ref。父/子separator只解码 `\\n/\\t/\\r`，中文自定义字符原样保存。
- [ ] **7. 跑 green 和原分段门。**

```bash
env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_markdown_chunking.py -q
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_ingestion.py -q
```

第一条为纯分段测试；第二条由现有 core runner 加载测试数据库配置并使用随机数据库 fixtures，要求零未说明跳过。最终有数据库的整体覆盖在 P4 运行。

- [ ] **8. 检查并交付。** A09/A10/A11/A29；所有新增 SourceSpan/AttachmentOccurrence 字段与总计划一致。

## P3-T3：配置快照、动态能力与文件身份

**Files**

- Modify: `backend/packages/knowledge/actweave_knowledge/contracts.py`、`module.py`、`documents/service.py`。
- Modify: `backend/packages/knowledge/actweave_knowledge/persistence/tasks.py`（reparse_settings校验/投影）。
- Modify: `backend/app/knowledge/config.py`、`composition.py`、`gateway.py`。
- Modify: M11 产出的 `backend/app/knowledge_settings/service.py`、`bootstrap.py`、`backend/app/gateway/routers/admin_knowledge_settings.py`；消费 `backend/packages/harness/deerflow/persistence/knowledge_settings/model.py` 的字段更新由 P2-T1 统一负责。
- Create: `backend/packages/knowledge/actweave_knowledge/ingestion/profiles.py`。
- Test: `backend/tests/knowledge/test_parsing_profiles.py`、`test_file_capabilities.py`；扩展 `test_upload.py`、`test_host_config.py`。

**Interfaces**

- Consumes: P1 registry、P2 schema、M11 PostgreSQL settings、P3-T1 tokenizer fingerprint。
- Produces: `preview_fingerprint(...)`、`resolve_processing_profile(settings, user_parameters, registry) -> ProcessingProfile`；总计划 §3.4 的 HTTP DTO。

- [ ] **1. 核对 M11 入口已存在并通过其定向测试。** `load_knowledge_settings_from_db` 是配置读取入口，`app/knowledge/config.py` 仅重导出，不再从 YAML 建立第二权威；summary handler 已实现。运行 `PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/test_knowledge_settings_postgres.py -q`，记录基线提交；未满足则保留已完成的独立模块，不接通生产流程。
- [ ] **2. 写 fingerprint red。**

```python
from actweave_knowledge.extraction.contracts import ProcessingProfile
from actweave_knowledge.ingestion.profiles import preview_fingerprint
from parsing_test_helpers import make_parse_profile, make_chunk_profile

def test_preview_identity_binds_source_bytes_and_both_profiles():
    profile = ProcessingProfile(parse=make_parse_profile('.pdf'), chunk=make_chunk_profile())
    args = {'extension': '.pdf', 'profile': profile, 'capability_revision': 'r1'}
    first = preview_fingerprint(source_sha256='a' * 64, **args)
    assert first != preview_fingerprint(source_sha256='b' * 64, **args)
    changed = profile.model_copy(update={'chunk': profile.chunk.model_copy(update={'size': 800})})
    assert first != preview_fingerprint(source_sha256='a' * 64, extension='.pdf', profile=changed, capability_revision='r1')
```

- [ ] **3. 跑 red 后实现规范摘要。**

```python
import hashlib
import json

def preview_fingerprint(*, source_sha256, extension, profile, capability_revision):
    payload = {'source_sha256': source_sha256, 'extension': extension.lower(),
               'profile': profile.model_dump(mode='json'), 'capability_revision': capability_revision}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
```

production函数补全总计划中的注解，并严格验证 source摘要与extension。上下游不在该函数里读文件；Gateway staging边读取边计算哈希，正式上传重新计算，不能相信客户端sha。

- [ ] **4. 接入有效系统设置和能力响应。** settings新增 etl_type/extraction_cache_enabled；值从M11 PostgreSQL行 materialize，重启生效。能力revision组合P1解析资源fingerprint+P3Tokenizer/splitter能力；只影响file能力/预览，不把Tokenizer放入P2解析缓存键。GET能力在事务里复验shared_assets.read；缺少依赖格式available=false+稳定reason_code，必需格式不就绪阻止启用。
- [ ] **5. 冻结上传/reparse/retry。** 服务器选择 extractor和版本，接收用户可配的分段/表头字段；同时传旧表单字段和processing_profile冲突字段则422，不静默决定优先级。收到expected_preview_fingerprint则核对后再创建uploading/PUT；未传则正常冻结当前配置。retry读取任务冻结profile；reparse新profile只有发布成功才写回Document。

首次上传冻结在Document.parsing_profile（Document创建时写）；普通ingest task的reparse_settings仍NULL。显式reparse使用已有Task.reparse_settings，在原有chunk字段之外加入processing_profile完整结构与capability_revision；服务端校验旧chunk投影与chunk profile逐项一致。同步 `persistence/tasks.py` 的reparse参数校验与claim投影，不能只修改pipeline读取。重试从相同来源取值，禁止重新materialize当前系统ETL。已发布但无profile的历史文档仅按character显示/重嵌入；若历史待解析任务没有可复现的解析版本则要求显式reparse，不伪称能够复现旧解析。
- [ ] **6. 测试headless与过期预览。** 使用现有upload fake store验证：文件B携带文件A fingerprint时Document数/对象数不增；无fingerprint正常入队；跨项目capability缓存不可复用；修改全局ETL后老queued任务仍使用原profile。

```bash
env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_parsing_profiles.py -q
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_file_capabilities.py tests/knowledge/test_upload.py tests/knowledge/test_host_config.py -q
```

- [ ] **7. 检查交付。** A01/A13/A25/A30；同步backend/AGENTS.md中配置来源、分隔符和参数冻结说明。

## P3-T4：无副作用预览接入

**Files**

- Modify: `backend/packages/knowledge/actweave_knowledge/ingestion/preview.py`、`module.py`、`documents/service.py`。
- Modify: `backend/app/knowledge/gateway.py` 的chunk-preview和reparse-preview DTO。
- Create: `backend/packages/knowledge/actweave_knowledge/ingestion/preview_assets.py`。
- Create: `backend/tests/knowledge/ingestion_test_helpers.py`；Modify: `backend/tests/knowledge/parsing_test_helpers.py`。
- Test: `backend/tests/knowledge/test_parsing_preview.py`、`test_ingestion.py`。

**Interfaces**

- Consumes: P1 run_extraction和LocalAttachment；P3 split_documents/fingerprint；已授权的原件下载供reparse预览。
- Produces: `make_preview_assets(assets, *, work_dir, selected_refs) -> tuple[list[dict], int]`；总计划preview DTO。新文件预览没有ExtractionStore写调用。

- [ ] **1. 把预览集成测试写在独立文件。** P1 parsing_test_helpers 增加 `write_docx_with_image(path)`，使用 python-docx 和内存PNG生成确定样例；不从其他测试模块取私有函数。

```python
# parsing_test_helpers.py：无远程资源的图文样例。
import io
from pathlib import Path
from docx import Document as WordFile
from PIL import Image

def write_docx_with_image(path: Path) -> None:
    document = WordFile()
    document.add_heading('设备手册', level=1)
    document.add_paragraph('管理接口地址为 10.0.0.1，请检查链路状态。')
    with io.BytesIO() as image_bytes:
        with Image.new('RGB', (8, 8), 'red') as image:
            image.save(image_bytes, format='PNG')
        image_bytes.seek(0)
        document.add_picture(image_bytes)
        document.save(path)
```

```python
import pytest
from actweave_knowledge.extraction.contracts import ProcessingProfile
from parsing_test_helpers import make_parse_profile, make_chunk_profile, write_docx_with_image
from ingestion_test_helpers import ingestion_harness

def snapshot_rows(tables):
    return {name: sorted(
        [tuple((column.name, repr(getattr(row, column.name))) for column in row.__table__.columns)
         for row in rows], key=repr
    ) for name, rows in tables.items()}

@pytest.mark.asyncio
async def test_preview_does_not_persist_anything(postgres_database_url, tmp_path):
    async with ingestion_harness(postgres_database_url) as h:
        path = tmp_path / 'manual.docx'
        write_docx_with_image(path)
        profile = ProcessingProfile(parse=make_parse_profile('.docx'), chunk=make_chunk_profile())
        before = snapshot_rows(await h.resources.read_rows())
        objects_before = dict(h.resources.object_store.objects)
        preview = await h.preview(path, profile)
        assert preview.preview_attachments
        assert snapshot_rows(await h.resources.read_rows()) == before
        assert h.resources.object_store.objects == objects_before
```

- [ ] **2. 建立 P3 共用 harness。** 在 `backend/tests/knowledge/ingestion_test_helpers.py` 把现有 `test_ingestion.py` 的假模型客户端/生成器迁为命名公开helpers，保留旧测试引用。`ingestion_harness` 复用 P2 extraction_harness 的真实随机PG/假对象存储和quota，yield对象包括 `resources`、公开module、fake_model和总计划列出的方法；upload/preview/reparse/reembed必须调用production对应service，不直接改行伪造结果。`run_next_task` 使用真实claim_next_task后调用production worker，`segments`通过session查询实际发布行。harness退出在finally释放engine与进程。
- [ ] **3. 运行 red，接入run_extraction。** Gateway经写权限检查后stage原件，使用进程级 `ParserSlots(1)` 的非排队上下文包围本次解析，槽满立即返回资源忙。创建PreviewAttachmentSink；on_asset只将已验证文件登记在当前临时目录映射，不调用P2persist。run_extraction guard复验调用者项目权限；完成后再次复验并切分。Worker 不叠加 Gateway 的单槽。
- [ ] **4. 限制响应图片并投影表头诊断。** 只为前10父段收集refs；顺序去重，最多20张。缩略图在内存安全转换后校验每张128KiB和总2MiB；不满足则省略并增加计数。逻辑ref使用原规范图摘要，不能用缩略图摘要改正文。响应只含base64字节和MIME，不含LocalAttachment.relative_path。CSV/Excel 的 `table_sources` 按总计划 DTO 从 P1 表头与行元数据生成；sheet保留原名，候选行使用真实行号，无标题推测时返回null，header_cells来自解析保留的列名。
- [ ] **5. 用try/finally保证清理。** 取消时await运行器终止子进程，再删除目录；不得异步丢弃运行器。重解析预览从已授权原件走临时解析，不调用需要Task pin的find_ready；本期不采用规格允许但不强制的预览缓存优化，以保持零持久化。临时解析和正式摄取仍共用P1规范化与P3切分。
- [ ] **6. 跑green与数量/超时/权限例。** 20张以上、多张超总额、撤权晚到响应、空文本、取消、坏图warnings都覆盖。

```bash
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_parsing_preview.py -q
```

- [ ] **7. 检查交付。** A12/A13/A17/A18/A23；记录“预览没有外部模型调用”，不能把OCR无意放入此路径。

## P3-T5：Worker 原子发布与统一索引输入

**Files**

- Modify: `backend/packages/knowledge/actweave_knowledge/ingestion/pipeline.py`、`progress.py`。
- Modify: `backend/packages/knowledge/actweave_knowledge/persistence/tasks.py`、`module.py`、`contracts.py`。
- Modify: `backend/app/knowledge/worker.py`、`composition.py`。
- Test: `backend/tests/knowledge/test_parsing_pipeline.py`、`test_task_progress.py`、`test_worker.py`。

**Interfaces**

- Consumes: P2 ExtractionStore及schema；P3 split_documents；既有 model_client.embed / progress.ensure_claim_alive。
- Produces: 改造后的KnowledgeIngestionHandler；行中的展示/索引/Token/来源/附件绑定和published_extraction_id同时发布。

- [ ] **1. 写预览/发布和缓存重试红测。**

```python
import pytest
from actweave_knowledge.extraction.contracts import ProcessingProfile
from parsing_test_helpers import make_parse_profile, make_chunk_profile, write_docx_with_image
from ingestion_test_helpers import ingestion_harness

@pytest.mark.asyncio
async def test_ingest_matches_preview_and_embeds_index_text(postgres_database_url, tmp_path):
    async with ingestion_harness(postgres_database_url) as h:
        path = tmp_path / 'manual.docx'
        write_docx_with_image(path)
        profile = ProcessingProfile(parse=make_parse_profile('.docx'), chunk=make_chunk_profile())
        preview = await h.preview(path, profile)
        uploaded = await h.upload(path, profile)
        await h.run_next_task()
        rows = await h.segments(uploaded.id)
        assert [r.content for r in rows[:10]] == [c.content for c in preview.chunks]
        assert h.fake_model.calls[-1] == [r.index_text for r in rows]
        assert all('knowledge-attachment:' not in t for t in h.fake_model.calls[-1])
```

- [ ] **2. 跑red，替换摄取阶段。** `_begin_processing`读取并验证已在准入时冻结的完整profile，不在执行时读取最新配置；reading_source后先检查缓存。miss则begin、传on_asset到run_extraction、complete；hit保留任务pin。缓存取回和解析前后都调用progress.ensure_claim_alive。normalize/split之后校验父段和子向量两种quota再请求Embedding。
- [ ] **3. 发布前构造所有派生值。** Parent/Child embedding输入只使用index_text；parent_child父向量仍NULL。每段word_count=len(content)，token_count按当前profile。内部表/属性名必须采用P2完成的schema，不再新增DDL。

在现有发布事务内的行构造关键代码：

```python
rows = [KnowledgeSegmentRow(
    id=segment_id, project_id=document.project_id, knowledge_base_id=document.knowledge_base_id,
    knowledge_document_id=document.id, document_version=document.version, position=draft.position,
    extraction_id=stored.extraction_id,
    content=draft.content, index_text=draft.index_text, token_count=draft.token_count,
    word_count=len(draft.content), source_position=draft.source_position,
    source_spans=[s.model_dump(mode='json') for s in draft.source_spans],
    embedding=None if parent_child else parent_vectors[index],
    lexical_tsv=func.to_tsvector('simple', lexical_index_input(draft.index_text)),
    lexical_version=KNOWLEDGE_LEXICAL_VERSION,
) for index, (segment_id, draft) in enumerate(zip(segment_ids, drafts, strict=True))]
session.add_all(rows)
await session.flush()
```

此段放在既有 `_publish` 已持Project/Task/Document锁且复验lease/version之后，消费同方法中定义的document、stored（StoredExtraction）、drafts、segment_ids及向量。随后按StoredExtraction所辖ref→attachment_id建立binding，插入Child行，更新Document.parsing_profile/published_extraction_id/published_version/status，最后结算Task并清pin。关系错误必须rollback全部，不能先commit图。
- [ ] **4. 保留失败隔离。** 解析失败清理本attempt未完成结果；Embedding失败保留完整ready缓存供retry；CAS失效不能发布/误删别的attempt。使用P2耐久cleanup入口，不在finally直接删所有图片。真实阶段只报告已验证量，不因缓存hit伪造页数。
- [ ] **5. 故障注入与green。** 在on_asset、manifest complete、embedding barrier、事务flush、最终commit各处注入故障，验证内容/附件pointer不混代、pin释放、临时目录和对象删除次序；失租约停止后续batch。

```bash
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_parsing_pipeline.py tests/knowledge/test_task_progress.py tests/knowledge/test_worker.py -q
```

- [ ] **6. 检查交付。** A11/A14/A15/A16/A17/A18/A19；本任务不能以“happy path上传成功”替代故障门。

## P3-T6：人工治理、reparse/reembed、摘要与检索

**Files**

- Modify: `backend/packages/knowledge/actweave_knowledge/segments/service.py`、`documents/service.py`。
- Modify: `backend/packages/knowledge/actweave_knowledge/ingestion/reembed.py`、`retrieval/service.py`、`persistence/derivations.py`。
- Modify: M11产出的 `backend/packages/knowledge/actweave_knowledge/ingestion/summarize.py`、`backend/app/knowledge/run_tool.py` 的安全引用投影（仅必要的新来源字段）。
- Test: `backend/tests/knowledge/test_parsing_governance.py`、`test_reembedding.py`、`test_retrieval.py`、`test_search_details.py`、`test_governance.py`。

**Interfaces**

- Consumes: P3派生逻辑，P2附件ref验证/读取，M11摘要调度。
- Produces: `_Candidate.index_text` 只供rerank；`content`继续供ToolMessage与content_digest；管理/引用保持不同可见性。

- [ ] **1. 写reembed保真及rerank输入红测。**

```python
import pytest
from actweave_knowledge.extraction.contracts import ProcessingProfile
from parsing_test_helpers import make_parse_profile, make_chunk_profile
from ingestion_test_helpers import ingestion_harness

@pytest.mark.asyncio
async def test_reembed_never_reextracts_or_changes_markdown(postgres_database_url, tmp_path):
    async with ingestion_harness(postgres_database_url) as h:
        path = tmp_path / 'guide.md'
        path.write_text('# 手册\n\n接口为 `List<int>`。', encoding='utf-8')
        profile = ProcessingProfile(parse=make_parse_profile('.md'), chunk=make_chunk_profile())
        doc = await h.upload(path, profile)
        await h.run_next_task()
        before = await h.segments(doc.id)
        saved = [(r.id, r.content, r.source_spans) for r in before]
        h.resources.object_store.fail_next('get')
        await h.reembed(doc.knowledge_base_id)
        await h.run_next_task()
        assert [(r.id, r.content, r.source_spans) for r in await h.segments(doc.id)] == saved
```

额外测试使用fake_model.rerank_calls（在harness中新增记录）：向reranker传入index_text，最终返回仍是带Markdown/逻辑图片ref的content且digest匹配。不要改现有rank融合、分数意义和预算算法。
- [ ] **2. 跑red，复用派生函数到人工编辑。** 在Gateway授权之后校验content和允许引用的本Document附件；新index_text、tokens、children在Embedding前算好；每次真实model batch/retry复验权限；事务再锁Document检查generation，原子更换内容/附件关系/children/lexical。禁用不删向量/图片。
- [ ] **3. 改reembed与reparse。** reembed用保存的index_text重新算向量，保留展示正文、source_spans、attachment bindings、parser/Tokenizer profile；禁止注入extractor/object_store。reparse用新确认profile，只有成功才替换全部内容与published_extraction_id；失败管理浏览可以读取旧published图片。
- [ ] **4. 接入M11派生契约。** 文本变化清除旧摘要并按库开关排队，新的摘要输入使用index_text；reembed不重新生成摘要，只重新嵌入已有摘要。缓存/附件标识不作为摘要正文，摘要不会替代引用原文。若M11实现名与计划不符，按其已通过测试的公开入口适配并更新Files记录，不能复制第二套调度。
- [ ] **5. 保持检索授权和引文精确性。** 所有general/child/lexical候选查询同时选取content和index_text；rerank改读index_text，但统一终审继续校验实际content_digest和版本。新增source_spans仅安全投影，ToolMessage仍≤64KiB整段装包，禁止偷偷截断。
- [ ] **6. 运行green和相关回归。**

```bash
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/test_parsing_governance.py tests/knowledge/test_reembedding.py tests/knowledge/test_retrieval.py tests/knowledge/test_search_details.py tests/knowledge/test_governance.py -q
```

- [ ] **7. 验收P3。** A11/A16/A17/A20/A21/A29/A30全部有证据后交给P4。更新backend/AGENTS.md与用户文档中单位/来源/缓存/手工修改语义；不把新的能力写成已上线。授权提交时逐文件暂存。

## P3 收尾记录

- [ ] 新纯函数测试、PG集成和旧knowledge门分别记录命令/计数；所有改动源文件格式化和lint通过。
- [ ] 向P4交接真实DTO样例、capability_revision、管理/引用附件路由、header_rules参数与旧character投影样例。
- [ ] 确认没有OCR、解析API、运行时模型/Tokenizer下载或不受保护的图片URL。
- [ ] 检查总计划的任务覆盖表与本文件P3-T1至P3-T6一致，记录输入基线和本任务变更范围。
