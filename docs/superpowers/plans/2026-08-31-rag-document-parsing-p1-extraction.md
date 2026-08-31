# RAG P1：解析模块与本地格式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可独立验证的纯本地解析包，保留 Dify 源码来源、Markdown 内容、完整来源位置、安全图片及离线执行证据，不切换生产摄取入口。

**Architecture:** 依照数据源、ETL、扩展名三级注册表调用固定版本 Adapter，输出统一 `Document`。子进程仅处理本地文件并通过受限 IPC 交付图片，父进程校验后调用注入回调；数据库、对象存储、授权和模型均由后续宿主编排负责。

**Tech Stack:** Python 3.12、Pydantic v2、Dify 固定源码、pypdfium2、python-docx、openpyxl、pandas/xlrd、BeautifulSoup、charset-normalizer、Pillow、Unstructured 本地库、pytest、asyncio subprocess。

**Spec:** [权威规格](../specs/2026-08-31-rag-document-parsing-design.md)，必须同时阅读[总计划 §3/§4 共用契约](2026-08-31-rag-document-parsing.md)。总计划的类型、字段和 helpers 是跨包接口权威；规格中的 `AttachmentDraft` 在实施中具体化为 `Attachment`、`AttachmentOccurrence`，不创建第三套类型。

## Global Constraints

- ActWeave 核对基线 `b96581974b057c0ae4d853815130d99c0ed23823`；Dify 源码固定 `9c16c865977e9d89a9ec7ae0536e893f4385a758`。
- 数据源本期只交付 `file`；ETL 枚举 `dify|unstructured_local`，默认 `dify`。没有 Unstructured API、URL 下载或运行时资源下载。
- `actweave_knowledge` 不得导入 `app.*`、`deerflow.*` 或 Dify 的 `models/extensions/core.file`。
- 原文件继续 ≤50 MiB，MinIO 每次单 PUT，复用现有每 store 单上传槽。
- 提取正文最多 5,000,000 字符；父段及实际向量条目分别不超过当前每文档 5,000 配额。
- 图片最多 100 个独立字节对象，每张 ≤5 MiB、≤20,000,000 像素，单文档图片合计 ≤50 MiB。
- manifest 规范 JSON ≤50 MiB；每次解析工作目录合计 ≤512 MiB。
- Tokenizer Profile ID `knowledge-cl100k-v1`；`cl100k_base` 数据构建时固定并校验，运行时不下载，不作为目标模型计费 Token。
- 新上传默认父段 1000 Token、overlap 100 Token、子块 500 Token、子块零重叠；父段 200..4000、overlap 0..500 且小于父段、子块 100..2000 且小于父段；父段仍受 4000 字符硬上限约束。
- 预览不写 PostgreSQL/MinIO/Task；前 10 父段、最多 20 张缩略图、每张 ≤128 KiB、合计 ≤2 MiB。前端 Blob URL 随作用域释放。
- 正式图片读取受服务器项目与分段绑定授权，不能下发 MinIO key/签名 URL；raw HTML、外部图片自动加载、脚本协议禁止。
- 预览解析 120 秒、每 Gateway 进程 1 个解析槽；Worker 沿用并发默认 2、总任务默认 900 秒。取消后先回收解析子进程、再排空已发出的对象 I/O。
- 当前 published extraction 不回收；至多保留一个完整未发布缓存 24 小时，数据库时间判定，活跃任务引用阻止回收。
- 新 schema 只在新空测试数据库安装；不运行目标库 reset/ALTER、启动补表或降级。需要保留已有数据库时，另行制定部署迁移方案。
- 本轮只生成计划，不执行代码变更、数据库操作或提交。执行时先使用 using-git-worktrees 建立隔离工作区；不得覆盖当前其他改动。
- 每任务包含 red→green 验证和 diff 检查；只有当时用户已授权提交时才提交该任务的明确文件，不使用 `git add -A` 或自动 push。
- P1 不实施 OCR、图片向量、Token 切分、持久化或生产入口替换。它们不能成为本地格式测试的隐式 fallback。
- TXT/Markdown/CSV 编码探测只读最多 1 MiB 样本，最多 5 秒，完整文件严格解码；auto 表头只扫描前 10 行。

---

## 文件与接口所有权

所有下列包路径均位于 `backend/packages/knowledge/actweave_knowledge/`；任务 Files 中仍给出完整仓库相对路径。

| 文件 | 单一职责 |
| --- | --- |
| `extraction/contracts.py`、`base.py`、`manifest.py` | 共用不可变 DTO、协议、稳定序列化及缓存解析 fingerprint |
| `extraction/registry.py`、`processor.py`、`signatures.py` | 唯一路由、准入/能力共同来源、容器签名预检 |
| `extraction/dify/*_extractor.py` | 固定版本移植及逐格式修正 |
| `extraction/encoding.py`、`tabular.py`、`normalizer.py` | 有限编码探测、表头和字段绑定、位置保持规范化 |
| `extraction/images.py`、`ipc.py` | 安全图片规范化及父进程文件接收 |
| `extraction/unstructured_local/*_extractor.py`、`elements.py` | 本地调用及实际元素 metadata 转换 |
| `extraction/runtime.py`、`child.py`、`runtime_resources.py` | 可终止隔离、子进程协议、运行时依赖资源清单 |
| `extraction/UPSTREAM.md`、`patches.md` | 原文件摘要、版本和补丁/依赖差异；不含宿主凭据 |
| `backend/tests/knowledge/parsing_test_helpers.py` | 总计划唯一解析测试 helper 来源 |

**已确认：** 当前测试使用 `backend/.venv/bin/python -m pytest`，`tests/conftest.py` 会隔离导入期数据库地址；纯解析测试不申请 PostgreSQL fixture。当前环境没有 Unstructured；当前 lock 和环境的 pypdfium2 为 5.7.1。Dify 的 5.6.0 是候选而非降级要求，优先验证当前锁定 5.7.1；不能把本机已有库当作完整格式矩阵通过证据。

**未验证：** 候选完整依赖在 Python 3.12/macOS 与生产 Linux 的可安装性、系统资源和隔离权限。P1-T2/T7/T8 为它们提供实际验证步骤；任何失败不能被“跳过”后标成格式通过。

下文测试命令都从 `backend/` 执行。`env -u DATABASE_URL .venv/bin/python -m pytest ...` 是纯测试，不安装依赖；`uv lock` / `uv sync` 明确是实施阶段有网络的依赖安装操作，不能夹在纯测试命令里。每个任务最后执行 `git diff --check` 和检查该任务 Files 的 diff；如有提交授权，只 stage 此任务文件并以任务 ID 提交，无授权不提交。

## P1-T1：固定 DTO、manifest 与跨包 helpers（A02、A14、A23）

**Files:**
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/__init__.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/contracts.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/base.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/manifest.py`
- Create: `backend/tests/knowledge/parsing_test_helpers.py`
- Create: `backend/tests/knowledge/test_extraction_contracts.py`

**Consumes:** 总计划 §3 全部字段；既有 `actweave_knowledge.contracts.KnowledgeError(code, message)`。

**Produces:** 总计划 §3 的所有 DTO、`BaseExtractor.extract(setting, context) -> list[Document]`、`AttachmentSink.accept(source_path, *, alt_text, source) -> Attachment`、`canonical_parse_fingerprint(profile) -> str`、`encode_manifest(result) -> bytes`、`decode_manifest(payload, limits) -> ExtractionResult`；总计划 §4 五个 helpers，外加下文明确命名的 `make_context(work_dir)`。

- [ ] **Step 1：加入可执行的 DTO 与缓存边界测试。**

```python
# backend/tests/knowledge/test_extraction_contracts.py
import json

import pytest
from pydantic import ValidationError
from actweave_knowledge.extraction.contracts import (
    Document, ExtractionLimits, ExtractionResult, ParseProfile, SourceSpan,
)
from actweave_knowledge.extraction.manifest import (
    canonical_parse_fingerprint, decode_manifest, encode_manifest,
)


def test_manifest_roundtrip_keeps_structure_and_has_no_path():
    profile = ParseProfile(etl_type="dify", extractor_id="dify.pdf",
        extractor_version="upstream-adapter-build", normalization_version="md-v1",
        image_policy_version="raster-v1", header_rules=())
    documents = tuple(Document(page_content=text,
        source_spans=(SourceSpan(block_id=f"page:{n}", start=0, end=len(text),
                                location={"page": n}),), kind="page")
        for n, text in enumerate(("第一页", "第二页"), 1))
    result = ExtractionResult(documents=documents, source_sha256="a" * 64,
        parse_fingerprint=canonical_parse_fingerprint(profile))
    payload = encode_manifest(result)
    assert decode_manifest(payload, ExtractionLimits()) == result
    assert [d.source_spans[0].location["page"] for d in result.documents] == [1, 2]
    assert payload == encode_manifest(decode_manifest(payload, ExtractionLimits()))
    assert b"relative_path" not in payload and b"source_path" not in payload
    with pytest.raises(ValidationError):
        Document(page_content="x", project_id="untrusted")


def test_manifest_enforces_current_budget_and_span_bounds():
    result = ExtractionResult(documents=(Document(page_content="abcd"),),
                              source_sha256="a" * 64, parse_fingerprint="b" * 64)
    with pytest.raises(Exception, match="KNOWLEDGE_QUOTA_EXCEEDED"):
        decode_manifest(encode_manifest(result), ExtractionLimits(max_text_chars=3))
    with pytest.raises(ValidationError):
        Document(page_content="a", source_spans=(SourceSpan(
            block_id="p:1", start=0, end=2, location={"paragraph": 1}),))
```

- [ ] **Step 2：运行 red。** `env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_extraction_contracts.py -q`。预期新增模块尚不存在；不得接受无关导入失败作为 red。

- [ ] **Step 3：实现 DTO 和协议。** 所有总计划字段逐个定义为 frozen Pydantic model，`ConfigDict(frozen=True, extra='forbid')`；`ExtractionContext` 另启用 `arbitrary_types_allowed=True` 以接收 `Path`/回调/协议实现，不能序列化成 manifest。`HeaderRule` explicit 必须 row≥1，其他模式拒绝非空 row；`SourceSpan` 满足 0≤start≤end，位置中的已知数字字段从 1 开始；`Document` 校验 span end≤len(page_content)，attachment source 同样校验。`Attachment.ref/source_sha256/parse_fingerprint` 均只接受 64 个小写十六进制字符。

```python
# contracts.py 的预算、基础模型及异常；其余 DTO 字段逐项采用总计划 §3。
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from actweave_knowledge.contracts import KnowledgeError, KNOWLEDGE_PARSE_FAILED

class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

class ExtractionLimits(FrozenModel):
    max_source_bytes: int = Field(default=50 * 1024**2, gt=0)
    max_text_chars: int = Field(default=5_000_000, gt=0)
    max_images: int = Field(default=100, gt=0)
    max_image_bytes: int = Field(default=5 * 1024**2, gt=0)
    max_image_pixels: int = Field(default=20_000_000, gt=0)
    max_total_image_bytes: int = Field(default=50 * 1024**2, gt=0)
    max_manifest_bytes: int = Field(default=50 * 1024**2, gt=0)
    max_work_dir_bytes: int = Field(default=512 * 1024**2, gt=0)

class ExtractionError(KnowledgeError):
    def __init__(self, reason_code: str, message: str = "文件解析失败") -> None:
        super().__init__(KNOWLEDGE_PARSE_FAILED, message)
        self.reason_code = reason_code
```

`ExtractionError` 继承现有错误，不改 HTTP 映射；P3 再公开 reason_code。正文等总体预算错误直接使用 `KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "解析资源超限")`。`Document.metadata` 是只读 property，返回 source_spans/heading_path/kind/warnings 的安全新投影，不接收任意 metadata 输入。`FrozenModel` 不是深冻结 dict 的证明：构造时复制 location，消费者不得就地改写，所有变换使用新实例。

`base.py` 用 `ABC`+`@abstractmethod` 定义总计划的 `BaseExtractor.extract`；`AttachmentSink` 为协议，禁止增加 store/session/tenant 参数。按总计划完整定义 `ParseProfile/ChunkProfile/ProcessingProfile`，即使 P1 尚不切分，也先锁定跨包签名。

共用字段的具体代码接在上述模型后；不要从 Dify Document 继承或放进任意 metadata：

```python
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Protocol, runtime_checkable
from pydantic import model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

class SourceSpan(FrozenModel):
    block_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    location: dict[str, str | int] = Field(default_factory=dict)
    role: Literal["source", "context_prefix"] = "source"

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end < self.start:
            raise ValueError("invalid source interval")
        return self

class ParseWarning(FrozenModel):
    code: str
    message: str
    source_position: dict[str, str | int] = Field(default_factory=dict)

class HeaderRule(FrozenModel):
    sheet: str | None = None
    mode: Literal["auto", "none", "explicit"] = "auto"
    row: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_row(self):
        if (self.mode == "explicit") != (self.row is not None):
            raise ValueError("explicit header requires a row")
        return self

class ParseProfile(FrozenModel):
    etl_type: Literal["dify", "unstructured_local"]
    extractor_id: str
    extractor_version: str
    normalization_version: str
    image_policy_version: str
    header_rules: tuple[HeaderRule, ...] = ()

class ChunkProfile(FrozenModel):
    unit: Literal["character", "token"]
    mode: Literal["general", "parent_child"]
    size: int
    overlap: int
    separator: str
    child_size: int
    child_separator: str
    remove_extra_spaces: bool
    remove_urls_emails: bool
    tokenizer_profile_id: str | None
    tokenizer_digest: str | None
    cleaner_version: str
    splitter_version: str

class ProcessingProfile(FrozenModel):
    parse: ParseProfile
    chunk: ChunkProfile

class Attachment(FrozenModel):
    ref: Digest
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

class LocalAttachment(FrozenModel):
    attachment: Attachment
    relative_path: str

class AttachmentOccurrence(FrozenModel):
    ref: Digest
    alt_text: str
    source: SourceSpan

class Document(FrozenModel):
    page_content: str
    source_spans: tuple[SourceSpan, ...] = ()
    heading_path: tuple[str, ...] = ()
    kind: str = "paragraph"
    attachments: tuple[AttachmentOccurrence, ...] = ()
    warnings: tuple[ParseWarning, ...] = ()

    @model_validator(mode="after")
    def validate_offsets(self):
        spans = self.source_spans + tuple(a.source for a in self.attachments)
        if any(s.end > len(self.page_content) for s in spans):
            raise ValueError("source interval outside content")
        return self

    @property
    def metadata(self):
        return dict(source_spans=[s.model_dump() for s in self.source_spans],
                    heading_path=list(self.heading_path), kind=self.kind,
                    warnings=[w.model_dump() for w in self.warnings])

class ExtractionResult(FrozenModel):
    documents: tuple[Document, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    warnings: tuple[ParseWarning, ...] = ()
    source_sha256: Digest
    parse_fingerprint: Digest

class ExtractSetting(FrozenModel):
    source_path: Path
    original_name: str
    datasource_type: Literal["file"] = "file"
    profile: ParseProfile

@runtime_checkable
class AttachmentSink(Protocol):
    def accept(self, source_path: Path, *, alt_text: str, source: SourceSpan) -> Attachment:
        raise NotImplementedError("protocol method")

class ExtractionContext(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    work_dir: Path
    sink: AttachmentSink
    limits: ExtractionLimits
    check_cancelled: Callable[[], None]
```

SourceSpan.location 与 ParseWarning.source_position 的接收校验允许已定义的 `page,paragraph,table,row,row_end,column,sheet,slide,chapter,line,line_end,element,image_index,table_path,encoding`；禁止任意 authority/path/URL 键，数字定位字段必须≥1。table_path 只允许数字和点分层级；sheet 是显示名称并按普通文本转义，不作为文件路径。

- [ ] **Step 4：实现 canonical JSON。** `encode_manifest` 封装 `{"format_version":1,"result":...}`，sort_keys/紧凑分隔符/ensure_ascii=False/allow_nan=False；解析 fingerprint 只包含 ParseProfile，依赖资源摘要归入 extractor_version，不含 ChunkProfile。

```python
# manifest.py 的关键完整函数
import hashlib
import json
from actweave_knowledge.contracts import KnowledgeError, KNOWLEDGE_QUOTA_EXCEEDED
from .contracts import ExtractionError, ExtractionLimits, ExtractionResult, ParseProfile


def canonical_parse_fingerprint(profile: ParseProfile) -> str:
    payload = json.dumps(profile.model_dump(mode="json"), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_result(result: ExtractionResult, limits: ExtractionLimits) -> None:
    assets = {a.ref: a for a in result.attachments}
    if len(assets) != len(result.attachments):
        raise ExtractionError("INVALID_MANIFEST")
    if (sum(len(d.page_content) for d in result.documents) > limits.max_text_chars
        or len(assets) > limits.max_images
        or sum(a.size_bytes for a in assets.values()) > limits.max_total_image_bytes
        or any(a.size_bytes > limits.max_image_bytes or
               a.width * a.height > limits.max_image_pixels for a in assets.values())):
        raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "解析资源超限")
    for document in result.documents:
        if any(item.ref not in assets for item in document.attachments):
            raise ExtractionError("INVALID_MANIFEST")


def encode_manifest(result: ExtractionResult) -> bytes:
    validate_result(result, ExtractionLimits())
    payload = json.dumps({"format_version": 1, "result": result.model_dump(mode="json")},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > ExtractionLimits().max_manifest_bytes:
        raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "解析资源超限")
    return payload


def decode_manifest(payload: bytes, limits: ExtractionLimits) -> ExtractionResult:
    if len(payload) > limits.max_manifest_bytes:
        raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "解析资源超限")
    try:
        envelope = json.loads(payload)
        if set(envelope) != {"format_version", "result"}:
            raise ValueError("manifest format")
    except (ValueError, TypeError):
        raise ExtractionError("INVALID_MANIFEST") from None
    if envelope["format_version"] != 1:
        raise ExtractionError("PARSER_PROFILE_UNAVAILABLE")
    try:
        result = ExtractionResult.model_validate(envelope["result"])
    except ValueError:
        raise ExtractionError("INVALID_MANIFEST") from None
    validate_result(result, limits)
    return result
```

再对逻辑图片链接与 attachments 做双向引用一致性校验：不在附件清单里的 ref 必须失败，清单无出现位置的 ref 必须失败；不能仅靠正则提取 URL 判断授权。未知 manifest 版本报 `PARSER_PROFILE_UNAVAILABLE`，损坏内容报安全解析错误，均不返回部分结果。

- [ ] **Step 5：写唯一公开 helpers。** registry 依赖在 helper 函数内部导入，P1-T1 的 DTO 测试直接构造 profile，P1-T2 后才调用 `make_parse_profile`。`CollectingAttachmentSink` 是纯测试接收器，不冒充生产图片规范化。

```python
# backend/tests/knowledge/parsing_test_helpers.py
import hashlib
from pathlib import Path
from actweave_knowledge.extraction.contracts import (
    Attachment, ChunkProfile, Document, ExtractSetting, ExtractionContext,
    ExtractionLimits, LocalAttachment, ParseProfile, SourceSpan,
)


def make_parse_profile(extension, *, etl_type="dify", header_rules=()):
    from actweave_knowledge.extraction.registry import default_registry
    item = default_registry().resolve(datasource_type="file", etl_type=etl_type,
                                      extension=extension)
    return ParseProfile(etl_type=etl_type, extractor_id=item.extractor_id,
        extractor_version=item.extractor_version, normalization_version="md-v1",
        image_policy_version="raster-v1", header_rules=tuple(header_rules))


def make_chunk_profile(**overrides):
    fields = dict(unit="token", mode="general", size=1000, overlap=100,
        separator="\\n\\n", child_size=500, child_separator="\\n\\n",
        remove_extra_spaces=False, remove_urls_emails=False,
        tokenizer_profile_id="knowledge-cl100k-v1", tokenizer_digest="a" * 64,
        cleaner_version="cleaner-v1", splitter_version="splitter-v1")
    fields.update(overrides)
    return ChunkProfile(**fields)


def make_document(text, *, location=None, heading_path=()):
    return Document(page_content=text, source_spans=(SourceSpan(block_id="block:1",
        start=0, end=len(text), location=location or {"paragraph": 1}),),
        heading_path=tuple(heading_path), kind="paragraph")


def make_setting(path, **overrides):
    fields = dict(source_path=Path(path), original_name=Path(path).name,
        datasource_type="file", profile=make_parse_profile(Path(path).suffix))
    fields.update(overrides)
    return ExtractSetting(**fields)


class CollectingAttachmentSink:
    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.assets = []
        self.occurrences = []

    def accept(self, source_path: Path, *, alt_text: str, source: SourceSpan) -> Attachment:
        from PIL import Image
        data = source_path.read_bytes()
        ref = hashlib.sha256(data).hexdigest()
        with Image.open(source_path) as image:
            width, height = image.size
        attachment = Attachment(ref=ref, media_type="image/png", size_bytes=len(data),
                                width=width, height=height)
        target = self.work_dir / f"{ref}.png"
        target.write_bytes(data)
        if not any(item.attachment.ref == ref for item in self.assets):
            self.assets.append(LocalAttachment(attachment=attachment, relative_path=target.name))
        self.occurrences.append((ref, alt_text, source))
        return attachment


def make_context(work_dir: Path) -> ExtractionContext:
    work_dir.mkdir(parents=True, exist_ok=True)
    return ExtractionContext(work_dir=work_dir, sink=CollectingAttachmentSink(work_dir),
                             limits=ExtractionLimits(), check_cancelled=lambda: None)
```

- [ ] **Step 6：运行 green，并确认不触碰现有 ingestion。** 重跑 Step 2；增加未知 JSON 字段、零/负预算、重复 ref、缺失 ref、路径字段泄漏样例到同一文件；`git diff --check`。此任务不迁移既有 `_write_pdf` 等 fixture，后续只新建本计划内公开生成器，避免跨测试模块导入私有方法。

## P1-T2：固定来源、安装候选、三级路由与签名（A01、A02、A25）

**Files:**
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/registry.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/processor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/signatures.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/UPSTREAM.md`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/patches.md`
- Modify: `backend/packages/knowledge/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/tests/knowledge/test_extractor_registry.py`

**Consumes:** P1-T1 DTO/BaseExtractor；固定 Dify `api/core/rag/extractor/` 及 `api/uv.lock`。

**Produces:** `ExtractorRegistration` 总计划字段；`ExtractorRegistry.resolve(*,datasource_type,etl_type,extension)`；`default_registry() -> ExtractorRegistry`；`ExtractProcessor(registry=None).extract(setting,context)`；`validate_file_signature(path,extension,limits) -> None`。registration.dependency_probe 为 `Callable[[], str|None]`，None 表示可用，reason_code 表示不可用；factory 为 `Callable[[], BaseExtractor]`。

- [ ] **Step 1：写三级路由与伪装容器测试。**

```python
# test_extractor_registry.py
import zipfile
import pytest
from actweave_knowledge.extraction.contracts import ExtractionError, ExtractionLimits
from actweave_knowledge.extraction.registry import default_registry
from actweave_knowledge.extraction.signatures import validate_file_signature

@pytest.mark.parametrize("etl", ["dify", "unstructured_local"])
@pytest.mark.parametrize("ext,parser", [
    (".txt", "dify.text"), (".pdf", "dify.pdf"), (".docx", "dify.word"),
    (".xlsx", "dify.excel"), (".xls", "dify.excel"), (".csv", "dify.csv"),
    (".html", "dify.html"), (".htm", "dify.html"),
    (".pptx", "unstructured.pptx"), (".epub", "unstructured.epub"),
])
def test_unique_routes(etl, ext, parser):
    item = default_registry().resolve(datasource_type="file", etl_type=etl, extension=ext.upper())
    assert item.extractor_id == parser

@pytest.mark.parametrize("ext", [".doc", ".ppt", ".odt", ".zip", ".exe"])
def test_disallowed_formats_never_fall_back(ext):
    for etl in ("dify", "unstructured_local"):
        with pytest.raises(ExtractionError) as caught:
            default_registry().resolve(datasource_type="file", etl_type=etl, extension=ext)
        assert caught.value.reason_code == "UNSUPPORTED_FORMAT"


def test_office_container_identity(tmp_path):
    path = tmp_path / "fake.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    with pytest.raises(ExtractionError) as caught:
        validate_file_signature(path, ".docx", ExtractionLimits())
    assert caught.value.reason_code == "FORMAT_SIGNATURE_MISMATCH"
```

补齐以下参数化输入及 assertions：`.md/.markdown/.mdx` 在两个模式分别唯一命中 dify.markdown/unstructured.markdown；EML/MSG/XML 仅 local；datasource=`web` 和未知 ETL 明确失败；空扩展名失败；依赖缺失返回 unavailable 且 processor 不调用 factory；ZIP 路径穿越、符号链接成员和解压总量超限失败。

- [ ] **Step 2：运行 red。** `env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_extractor_registry.py -q`。

- [ ] **Step 3：记录上游文件摘要与补丁矩阵。** 只复制本任务列明的解析器及所需局部 helper，不复制整套后端。使用下面脚本输出表格并将真实输出写入 `UPSTREAM.md`；不要预填猜测 SHA。

```bash
python3 - <<'PY'
import hashlib, subprocess
from pathlib import Path
root = Path('/Users/jiangfeng/dify')
commit = '9c16c865977e9d89a9ec7ae0536e893f4385a758'
files = ['extractor_base.py','helpers.py','text_extractor.py','markdown_extractor.py',
         'pdf_extractor.py','word_extractor.py','excel_extractor.py','csv_extractor.py',
         'html_extractor.py']
files += ['unstructured/unstructured_' + name + '_extractor.py'
          for name in ('pptx','epub','markdown','eml','msg','xml')]
for name in files:
    path = 'api/core/rag/extractor/' + name
    data = subprocess.check_output(['git','-C',str(root),'show',commit + ':' + path])
    print('|',path,'|',hashlib.sha256(data).hexdigest(),'|')
PY
```

`patches.md` 每项记录原文件→本地文件、补丁理由和验证节点：宿主 import/存储移除、缓存移交 P2、禁止外链/API/download、MD 字面值、Word 顺序/嵌套/空格、Excel 空列与定位、CSV 字符串与坏行、PPTX 无页码、邮件重复解码删除。保留上游代码结构和可比对函数，不借适配进行无关重写。DOC/PPT 的上游 API 路径明确排除；ODT 不支持；PPTX/EPUB 两模式保留。

- [ ] **Step 4：在实施工作区安装确切候选并生成锁文件。** 在 knowledge 包声明下列 exact pins，保留原有与解析无关依赖；旧 pypdf/ebooklib 暂留给旧 profile 路径，P3 完成兼容审查前不删除。Unstructured 使用已在上游 lock 核实存在的 epub/md/pptx extras，不写 all-docs，也不虚构 eml/xml extras。

```toml
# 将当前同名解析依赖替换为这些版本，并加入新增主流依赖。
# 其余既有依赖保持原定义。
# dependencies 内：
# beautifulsoup4==4.14.3
# charset-normalizer==3.4.7
# markdown-it-py==4.2.0
# openpyxl==3.1.5
# pandas==3.0.2
# pillow==12.3.0
# pypdfium2==5.7.1
# python-docx==1.2.0
# python-pptx==1.0.2
# xlrd==2.0.2

[project.optional-dependencies]
extraction-local = [
    "unstructured[epub,md,pptx]==0.21.5",
    "python-oxmsg==0.0.2",
    "pypandoc-binary==1.17",
    "python-magic==0.4.27",
]
```

安装命令（有网络，不是测试）：`uv lock`，随后 `uv sync --all-packages --extra extraction-local`。安装后执行 `uv pip check` 和 metadata 版本打印；若冲突记录 resolver 输出并在源码/规格审阅后调整候选，不能直接放宽到 `>=`。生产需要两模式中的 PPTX/EPUB，因此正式镜像必须安装该 extra；未装 extra 的开发环境只能显示明确不可用，不能切新摄取入口。

- [ ] **Step 5：实现显式登记及准入链。** 下列代码是 resolve 的核心，不使用默认 text fallback：

```python
# registry.py
from dataclasses import dataclass
from collections.abc import Callable
from .base import BaseExtractor
from .contracts import ExtractionError

@dataclass(frozen=True)
class ExtractorRegistration:
    extractor_id: str
    extractor_version: str
    extensions: tuple[str, ...]
    etl_types: tuple[str, ...]
    supports_embedded_images: bool
    factory: Callable[[], BaseExtractor]
    dependency_probe: Callable[[], str | None]

class ExtractorRegistry:
    def __init__(self, registrations: tuple[ExtractorRegistration, ...]):
        self.registrations = registrations
        self._routes = {}
        for item in registrations:
            for etl in item.etl_types:
                for ext in item.extensions:
                    key = (etl, ext)
                    if key in self._routes:
                        raise ValueError("duplicate extractor route")
                    self._routes[key] = item

    def resolve(self, *, datasource_type: str, etl_type: str,
                extension: str) -> ExtractorRegistration:
        if datasource_type != "file" or etl_type not in {"dify", "unstructured_local"}:
            raise ExtractionError("UNSUPPORTED_FORMAT")
        item = self._routes.get((etl_type, extension.lower()))
        if item is None:
            raise ExtractionError("UNSUPPORTED_FORMAT")
        return item
```

`default_registry()` 依照 Step 1 矩阵建立 14 个登记组（共享扩展名放同一组，但 XLS/XLSX 因 embedded_images 不同分开登记，共用 dify.excel ID/工厂），factory 内部才 import 具体 Adapter。表内 ID 对应本计划各类：`TextExtractor/MarkdownExtractor/PdfExtractor/WordExtractor/ExcelExtractor/CSVExtractor/HtmlExtractor` 与 `UnstructuredPPTXExtractor/UnstructuredEpubExtractor/UnstructuredMarkdownExtractor/UnstructuredEmlExtractor/UnstructuredMsgExtractor/UnstructuredXmlExtractor`。版本由固定 upstream commit、adapter revision、P1-T7 runtime 资源摘要组合；P1-T7 前以纯依赖 metadata 指纹构建，最终交付前补资源指纹并重新冻结 fixtures。

`ExtractProcessor.extract` 的执行顺序：resolve→确认 profile 的 ID/version 与登记一致（否则 `PARSER_PROFILE_UNAVAILABLE`）→probe（否则 `PARSER_DEPENDENCY_UNAVAILABLE`）→source ≤50 MiB→signature→factory.extract→检查累计字符预算。它不执行第二阶段切分，不生成 Token，不用返回图片 ref 代替实际文字判断。

`signatures.py` 对 PDF 检查 `%PDF-`；OOXML 检查 ZIP、`[Content_Types].xml` 的实际 MIME 声明和对应主部件 `word/document.xml`、`xl/workbook.xml`、`ppt/presentation.xml`；EPUB 检查 mimetype=`application/epub+zip` 和 `META-INF/container.xml`；XLS/MSG 检查 OLE magic 后仍由对应格式库校验流结构；TXT/MD/CSV/HTML/XML/EML 拒绝 NUL 二进制（有 UTF-16 BOM 除外）和 ZIP/OLE/PDF 签名，不执行文档。ZIP 在格式库加载前检查累计声明解压量≤512 MiB，不解包绝对路径、`..` 或 symlink，不能宣称这等于峰值内存上限。

- [ ] **Step 6：运行 green 与来源依赖边界检查。** 重跑 Step 2；`rg -n '^(from|import) (app|deerflow|models|extensions|configs|core)' packages/knowledge/actweave_knowledge/extraction` 应没有匹配；`git diff --check`。记录安装是否成功，不能把仅 resolve 通过算作格式解析通过。

## P1-T3：文本编码、Markdown 内容保护和安全 HTML（A04、A06、A11）

**Files:**
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/encoding.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/normalizer.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/dify/__init__.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/dify/text_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/dify/markdown_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/dify/html_extractor.py`
- Create: `backend/tests/knowledge/test_dify_text_extractors.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/patches.md`

**Consumes:** P1-T1 helpers/DTO；P1-T2 registry；原 `text_extractor.py/markdown_extractor.py/html_extractor.py/helpers.py`。

**Produces:** `decode_text_file(path:Path) -> tuple[str,str,tuple[ParseWarning,...]]`；`normalize_documents(documents:list[Document]) -> list[Document]`；`TextExtractor/MarkdownExtractor/HtmlExtractor`；`html_to_documents(markup:bytes|str) -> list[Document]`；`markdown_sections(text:str, *, encoding:str='utf-8') -> list[Document]`。三个 Extractor 均实现总计划接口。

- [ ] **Step 1：写真实编码及字面值测试。**

```python
# test_dify_text_extractors.py
import pytest
from actweave_knowledge.extraction.processor import ExtractProcessor
from actweave_knowledge.extraction.encoding import decode_text_file
from actweave_knowledge.extraction.normalizer import normalize_documents
from parsing_test_helpers import make_context, make_setting

@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "gb18030"])
def test_text_decode_is_lossless(tmp_path, encoding):
    path = tmp_path / "sample.txt"
    text = "网络接口 编号00123\n中文，不能丢失。" * 10
    path.write_bytes(text.encode(encoding))
    decoded, selected, warnings = decode_text_file(path)
    assert decoded == text
    assert selected
    if encoding == "gb18030":
        assert any(w.code == "ENCODING_DETECTED" for w in warnings)


def test_markdown_keeps_generics_hash_and_fences(tmp_path):
    path = tmp_path / "sample.md"
    text = "# C#\n父说明\n## 子节\nList<int> Map<K,V> <IP>\n```cpp\nvector<int> x;\n```\n"
    path.write_text(text, encoding="utf-8")
    docs = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert any(d.heading_path == ("C#", "子节") for d in docs)
    joined = "\n".join(d.page_content for d in docs)
    for literal in ("C#", "List<int>", "Map<K,V>", "<IP>", "vector<int> x;"):
        assert literal in joined
    assert "```cpp\nvector<int> x;\n```" in joined
    assert normalize_documents(normalize_documents(docs)) == normalize_documents(docs)
    for doc in docs:
        assert all(0 <= s.start <= s.end <= len(doc.page_content) for s in doc.source_spans)
```

- [ ] **Step 2：运行 red。** `env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_dify_text_extractors.py -q`。

- [ ] **Step 3：先移植解码顺序，再限制探测预算。** BOM UTF-8/UTF-16→严格 UTF-8→charset-normalizer 对最多 1 MiB 采样。探测函数只做 `charset_normalizer.from_bytes(sample).best()`；在当前解析子进程内用 POSIX `setitimer(ITIMER_REAL, 5)` 限制该调用，并在 finally 恢复原 handler/timer；禁止不可终止的 ThreadPool timeout。Mac/Linux 解析子进程主线程执行此函数，主进程和异步事件循环不得直接调用有 signal 的探测分支。探测后必须严格解码完整文件；空候选/超时/失败均 `ExtractionError('TEXT_DECODING_FAILED')`，消息不能带路径或字节。

```python
# encoding.py：预算有限且可恢复的探测器
import signal
from charset_normalizer import from_bytes
from .contracts import ExtractionError


def detect_encoding(sample: bytes) -> str:
    def expired(signum, frame):
        raise ExtractionError("TEXT_DECODING_FAILED", "编码探测超时")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        candidate = from_bytes(sample[:1024 * 1024]).best()
        if candidate is None or candidate.encoding is None:
            raise ExtractionError("TEXT_DECODING_FAILED")
        return candidate.encoding
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
```

`decode_text_file` 分块读取源文件，累计≤50 MiB；不使用 errors=ignore/replace。成功选择的 encoding 写入各 Document span.location['encoding']，探测时附 `ENCODING_DETECTED` warning。encoding 是安全元数据，不是页/行定位。换行从 CRLF/CR→LF 的变换在 source_spans 创建之前完成，行位置来自原始行序号，之后不得盲目 strip 整个正文。

- [ ] **Step 4：修正 Markdown 并写可追踪规范化。** 保留上游逐行标题/围栏组织，删除 `re.sub(r'<.*?>',...)` 和对完整标题的全局删 `#`。标题只消除开头的 ATX 标记；识别 backtick/tilde、最多 3 个前导空格和闭合长度≥开启长度；围栏内不识别标题。`markdown_sections` 返回每个标题节、祖先 heading_path、逐原始行 span（block_id=`line:<一基行号>`）。祖先标题不在当前片段内时仅保留 heading_path，P3 再插入 context_prefix，不提前伪造偏移。

```python
# markdown_extractor.py 中标题识别的关键实现
import re

ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def parse_heading(line: str) -> tuple[int, str] | None:
    match = ATX.match(line)
    if match is None:
        return None
    title = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2))
    return len(match.group(1)), title
```

`normalize_documents` 必须保持无变更输入的 byte-equivalent 文本和全部 span，不清除泛型、代码、不可见于 renderer 的普通字面尖括号。仅转换受控格式产物（换行、安全链接和明确的 HTML 元素），每次替换通过原区间→新区间更新 offset；可先在格式 Adapter 内生成规范文本，再生成 span，从而减少二次变换。外链 Markdown 图片改为可见“外部图片未获取”占位和 `EXTERNAL_IMAGE_NOT_FETCHED` warning；不得请求 URL，也不得删光 alt。生成的外图占位span标记context_prefix并指向原图片位置，不冒充source正文；安全 HTML 渲染属于 P4，P1 保留 Markdown/MDX 原文但不执行。

- [ ] **Step 5：移植 HTML 的真实结构转换并补测试。** BeautifulSoup 使用 HTML/EPUB 自身声明编码，不先套通用猜编码；删除 script/style/iframe/object/embed 和事件属性；仅保留 http/https/mailto 安全链接及其文字，javascript/data/file 协议只留可见文字；不访问 img src，输出 warning 和占位；保留 h1–h6/list/table/pre/code 有序内容。`html_to_documents` 对每个有效正文块分配 `block_id='html:<序号>'`，location 只有可证实块序号，不捏造页面。

```python
# 追加到 test_dify_text_extractors.py
from actweave_knowledge.extraction.dify.html_extractor import html_to_documents


def test_html_drops_active_content_but_keeps_code_and_link_label():
    docs = html_to_documents('<h1>标题</h1><script>SECRET()</script>'
        '<pre>List&lt;int&gt;</pre><a href="javascript:alert(1)">查看</a>'
        '<img src="https://tracker.invalid/x" alt="拓扑图">')
    text = "\n".join(d.page_content for d in docs)
    assert "List<int>" in text and "查看" in text
    assert "SECRET()" not in text and "javascript:" not in text
    assert any(w.code == "EXTERNAL_IMAGE_NOT_FETCHED" for d in docs for w in d.warnings)
```

- [ ] **Step 6：运行 green 和内容定位检查。** 重跑 Step 2；补不闭合围栏、tilde 围栏、MDX 表达式原文、未知/截断 BOM 和完整文件尾部坏编码样例。每个变换测试比较实际 covered slice，不能只比较 span 数；`git diff --check`。

## P1-T4：CSV/Excel 字符串、表头和真实行位置（A03、A04、A09）

**Files:**
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/tabular.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/dify/csv_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/dify/excel_extractor.py`
- Create: `backend/tests/knowledge/test_dify_tabular_extractors.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/patches.md`

**Consumes:** P1-T3 `decode_text_file`；`HeaderRule`；源 Excel/CSV Adapter。图片实际接收调用 P1-T5，当前任务用 `CollectingAttachmentSink` 验证调用位置，不独立实现第二套图片处理。

**Produces:** `CSVExtractor/ExcelExtractor`；`select_header(rows:list[list[object]], rule:HeaderRule) -> int|None`（返回一基行号）；`column_labels(values:list[object]) -> list[str]`；`rows_to_documents(rows:list[tuple[int,int,list[object]]], *, sheet:str|None, rule:HeaderRule) -> list[Document]`（前两项为原始起/止行）；每个数据行一个 Document，字段名和值绑定。

- [ ] **Step 1：加入字符串、坏行、空列及表头上下文样例。**

```python
# test_dify_tabular_extractors.py
import pytest
from actweave_knowledge.extraction.contracts import HeaderRule, ExtractionError
from actweave_knowledge.extraction.processor import ExtractProcessor
from parsing_test_helpers import make_context, make_parse_profile, make_setting


def test_csv_preserves_strings_and_multiline_locations(tmp_path):
    path = tmp_path / "values.csv"
    path.write_text('编号,标记,说明\n00123,NA,"上行\n下行"\n00004,,正常\n', encoding="utf-8")
    profile = make_parse_profile(".csv", header_rules=(HeaderRule(sheet=None, mode="explicit", row=1),))
    docs = ExtractProcessor().extract(make_setting(path, profile=profile), make_context(tmp_path / "work"))
    data = [d for d in docs if d.kind == "table_row"]
    assert len(data) == 2
    assert "00123" in data[0].page_content and "NA" in data[0].page_content
    assert "上行\n下行" in data[0].page_content
    assert data[0].source_spans[-1].location["row"] == 2
    assert data[0].source_spans[-1].location["row_end"] == 3
    assert data[1].source_spans[-1].location["row"] == 4


def test_csv_bad_row_fails_instead_of_disappearing(tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text("a,b\n1,2,3\n", encoding="utf-8")
    with pytest.raises(ExtractionError) as caught:
        ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert caught.value.reason_code == "CSV_ROW_INVALID"


def test_excel_blank_header_column_keeps_data_and_source(tmp_path):
    from openpyxl import Workbook
    path = tmp_path / "values.xlsx"
    wb = Workbook(); ws = wb.active; ws.title = "设备"
    for row in [["设备清单"], [], ["编号", None, "编号"], ["00123", "不能丢", "B"]]:
        ws.append(row)
    wb.save(path); wb.close()
    docs = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    row = next(d for d in docs if d.kind == "table_row")
    assert "列 B" in row.page_content and "不能丢" in row.page_content
    assert "编号 [A]" in row.page_content and "编号 [C]" in row.page_content
    assert any(s.location.get("row") == 4 for s in row.source_spans)
    assert any(s.location.get("row") == 3 and s.role == "context_prefix" for s in row.source_spans)
    assert any("设备清单" in d.page_content for d in docs)
    assert any(w.code == "HEADER_INFERRED" for d in docs for w in d.warnings)
```

- [ ] **Step 2：运行 red。** `env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_dify_tabular_extractors.py -q`。

- [ ] **Step 3：实现确定表头规则；空列宽度从全部有效数据列计算。** 不再复用上游“非空最多业务行兜底”或只留下非空表头列的 column_map。行号从 1 起，explicit 必须在范围内；none 完全不吃首行。auto 只取前 10 行至少两个非空字符串单元格的首候选，并附 warning。重复名使用列字母消歧，空列补 `列 B` 等。

```python
# tabular.py 关键完整算法
from openpyxl.utils.cell import get_column_letter
from .contracts import ExtractionError, HeaderRule


def select_header(rows: list[list[object]], rule: HeaderRule) -> int | None:
    if rule.mode == "none":
        return None
    if rule.mode == "explicit":
        if rule.row is None or rule.row > len(rows):
            raise ExtractionError("HEADER_ROW_INVALID")
        return rule.row
    for number, row in enumerate(rows[:10], 1):
        if sum(isinstance(value, str) and bool(value.strip()) for value in row) >= 2:
            return number
    return None


def column_labels(values: list[object]) -> list[str]:
    names = [str(v).strip() if v is not None and str(v).strip() else
             f"列 {get_column_letter(i)}" for i, v in enumerate(values, 1)]
    return [f"{name} [{get_column_letter(i)}]" if names.count(name) > 1 else name
            for i, name in enumerate(names, 1)]
```

`rows_to_documents` 先保留表头前非空行为 context Document，原表头作为 `kind='table_header'` 保存，每个原始单元格独立span并记录column位置，原值含空值完整保留，供P3投影表头预览；数据行 Markdown 为 `- 列名: 值`。重复列名前缀的 SourceSpan.role=context_prefix 指回 header 原行，值的 span.role=source 指当前数据行/列；没有真实表头时稳定列标签属于生成上下文，source 位置沿当前行/列，不伪造 header row。Excel 的 location 含 sheet,row,column；CSV 的 row/row_end 是物理行范围，包含 quoted 多行。空值输出空字符串而非 `None/nan`；内嵌换行保留，必要转义不破坏原值。

- [ ] **Step 4：移植 CSV 行生成，并用 strict csv 校验替换 pandas 的跳行/类型推断。** 这是局部数据完整性补丁，保留上游 Adapter 结构和输出职责，不把 CSV 变成新 ingestion。

```python
# csv_extractor.py 内部输入读取；后续调用 rows_to_documents。
import csv
import io
from ..contracts import ExtractionError


def read_csv_rows(text: str) -> list[tuple[int, int, list[str]]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows = []
    previous_end = 0
    try:
        for row in reader:
            start, end = previous_end + 1, reader.line_num
            previous_end = end
            rows.append((start, end, row))
    except csv.Error:
        raise ExtractionError("CSV_ROW_INVALID", "CSV 引号或字段格式无效") from None
    return rows


def validate_csv_width(rows: list[tuple[int, int, list[str]]],
                       header_index: int | None) -> None:
    # header_index 是逻辑记录一基序号，映射到物理行号使用 rows 的 start/end。
    begin = header_index - 1 if header_index is not None else 0
    records = [record for record in rows[begin:] if record[2]]
    if not records:
        return
    expected_width = len(records[0][2])
    if any(len(record[2]) != expected_width for record in records):
        raise ExtractionError("CSV_ROW_INVALID", "CSV 行列数不一致")
```

先按 HeaderRule 找表头再调用 validate_csv_width：已确认表头之前的非空说明记录保留为上下文，只有表头及其之后的数据记录要求列数一致；none 模式从第一条非空记录校验，不把坏行猜成备注。explicit 的 row 是物理起始行号，应先通过 rows.start 找到逻辑 header_index；auto 仍仅查看原始前10行，不把 quoted 多行当多条记录。CSV 全程字符串，没有 pandas 默认 NA 语义，也不手动 `.strip()` 数据值。

- [ ] **Step 5：移植 Excel 并绑定原行与图片锚点。** XLSX 使用 openpyxl `data_only=True, read_only=False`，另以 `data_only=False` 检查公式；公式存在但缓存缺失时保持空值并附 `FORMULA_CACHE_MISSING`，不计算公式。XLS 使用 pandas `header=None,dtype=object,keep_default_na=False` 或 xlrd 原单元格读取，行号使用原表格索引+1；不 dropna 后重新编号。图片遍历所有 sheet._images 锚点，不因表头列空、图片在说明行、重复 SHA 而丢出现位置；`.xls` supports_embedded_images=false。所有 workbook 在 finally close。

- [ ] **Step 6：补边界样例并运行 green。** 在此测试文件增加 header none/explicit、全数值无可信表头、多个 sheet、前置空行、CSV quote 内逗号/换行、空字段、公式缓存、XLS 原行号与同图不同锚点断言；前述测试代码为重点最小样例，不替代完整格式矩阵。重跑 Step 2；`git diff --check`。

## P1-T5：安全栅格规范化与父进程 IPC 接收（A07、A15、A18、A23）

**Files:**
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/images.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/ipc.py`
- Create: `backend/tests/knowledge/test_extraction_images.py`

**Consumes:** `Attachment/LocalAttachment/AttachmentOccurrence/SourceSpan/ParseWarning`；`AttachmentSink.accept`；本次工作目录。

**Produces:** `LocalAttachmentSink(work_dir:Path, limits:ExtractionLimits)`（有 assets/warnings）；`normalize_image(source_path:Path, target_dir:Path, limits:ExtractionLimits) -> LocalAttachment`；`ImageRejected(warning:ParseWarning)`；`receive_asset(asset:LocalAttachment, *, work_dir:Path, limits:ExtractionLimits, accepted:dict[str,Attachment]) -> LocalAttachment`。receive_asset 同步执行有限文件校验/复制，由 async 父进程 `asyncio.to_thread` 调用；on_asset 仍是总计划唯一异步外部回调。

- [ ] **Step 1：加入内容哈希去重、元数据清除、路径逃逸测试。**

```python
# test_extraction_images.py
from pathlib import Path
import pytest
from PIL import Image, PngImagePlugin
from actweave_knowledge.extraction.contracts import ExtractionError, ExtractionLimits, SourceSpan
from actweave_knowledge.extraction.images import LocalAttachmentSink, ImageRejected
from actweave_knowledge.extraction.ipc import receive_asset


def test_normalization_deduplicates_bytes_but_keeps_occurrences(tmp_path):
    source = tmp_path / "source.png"
    info = PngImagePlugin.PngInfo(); info.add_text("author", "private author")
    Image.new("RGB", (8, 8), "red").save(source, pnginfo=info)
    sink = LocalAttachmentSink(tmp_path / "child", ExtractionLimits())
    first = sink.accept(source, alt_text="页1", source=SourceSpan(
        block_id="p:1:image:1", start=0, end=0, location={"page":1}))
    second = sink.accept(source, alt_text="页2", source=SourceSpan(
        block_id="p:2:image:1", start=0, end=0, location={"page":2}))
    assert first.ref == second.ref and len(sink.assets) == 1
    with Image.open(sink.work_dir / sink.assets[0].relative_path) as result:
        assert "author" not in result.info
    # 出现位置由调用方写入两个 Document.attachments，sink 仅去重字节。


def test_oversized_pixels_reject_before_full_decode(tmp_path):
    source = tmp_path / "pixels.png"
    Image.new("RGB", (11, 10), "red").save(source)
    sink = LocalAttachmentSink(tmp_path / "child", ExtractionLimits(max_image_pixels=100))
    with pytest.raises(ImageRejected) as caught:
        sink.accept(source, alt_text="图", source=SourceSpan(
            block_id="image:1", start=0, end=0, location={"page":1}))
    assert caught.value.warning.code == "IMAGE_LIMIT_EXCEEDED"


def test_parent_rejects_symlink_in_any_path_component(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    work = tmp_path / "work"; work.mkdir()
    source = outside / "image.png"; Image.new("RGB", (2, 2)).save(source)
    producer = LocalAttachmentSink(outside / "normalized", ExtractionLimits())
    producer.accept(source, alt_text="图", source=SourceSpan(
        block_id="image:1", start=0, end=0, location={}))
    asset = producer.assets[0]
    (work / "child").symlink_to(producer.work_dir, target_is_directory=True)
    forged = asset.model_copy(update={"relative_path": "child/" + asset.relative_path})
    with pytest.raises(ExtractionError) as caught:
        receive_asset(forged, work_dir=work, limits=ExtractionLimits(), accepted={})
    assert caught.value.reason_code == "PARSER_OUTPUT_INVALID"
```

- [ ] **Step 2：运行 red。** `env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_extraction_images.py -q`。

- [ ] **Step 3：规范化在解码前检查像素，再首帧/去 metadata/确定字节。** 不透传 SVG、动画或原始图片。静态编码统一 PNG；RGB/RGBA/灰度转换保持可见内容，EXIF 方向应用后删除元数据。GIF/TIFF/WebP 多帧只取首帧并给 `IMAGE_FIRST_FRAME_ONLY` warning；不能在失败时把原图 URL 拼回正文。

```python
# images.py 核心；sink 将不含位置的 warning 用本次 source.location 具体化。
import hashlib
import io
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError
from .contracts import Attachment, LocalAttachment, ExtractionLimits, ParseWarning

class ImageRejected(Exception):
    def __init__(self, warning: ParseWarning):
        super().__init__(warning.code)
        self.warning = warning


def normalize_image(source_path: Path, target_dir: Path,
                    limits: ExtractionLimits) -> LocalAttachment:
    try:
        with Image.open(source_path) as opened:
            width, height = opened.size
            if width * height > limits.max_image_pixels:
                raise ImageRejected(ParseWarning(code="IMAGE_LIMIT_EXCEEDED",
                    message="图片像素超过上限", source_position={}))
            opened.seek(0)
            frame = ImageOps.exif_transpose(opened).convert("RGBA")
            clean = Image.new("RGBA", frame.size)
            clean.paste(frame)
            output = io.BytesIO()
            clean.save(output, format="PNG", compress_level=9, optimize=False)
            data = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ImageRejected(ParseWarning(code="IMAGE_CORRUPT",
            message="图片无法安全解码", source_position={})) from None
    if len(data) > limits.max_image_bytes:
        raise ImageRejected(ParseWarning(code="IMAGE_LIMIT_EXCEEDED",
            message="图片字节超过上限", source_position={}))
    ref = hashlib.sha256(data).hexdigest()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (ref + ".png")
    if not target.exists():
        with target.open("xb") as stream:
            stream.write(data)
    return LocalAttachment(attachment=Attachment(ref=ref, media_type="image/png",
        size_bytes=len(data), width=clean.width, height=clean.height), relative_path=target.name)
```

完整实现将 Pillow DecompressionBombWarning 提升为受控拒绝，原始文件读入和转换临时空间受工作目录预算约束；提前关闭 frame/clean/output。LocalAttachmentSink 在 normalize 成功后按 ref 判断是否已收录，仅新字节计入 max_images/max_total_image_bytes；超限先删除本次未接收临时产物，再抛 ImageRejected。`accept` 返回 Attachment，Adapter 根据最终 Markdown 占位长度创建 occurrence.source 的正确 start/end；source 参数提供原位置，不用尚未知长度的 span 作为最终输出。

- [ ] **Step 4：父进程重新验证，不相信子进程描述。** `receive_asset` 限定相对路径仅在 child 输出目录；逐组件 `openat`+`O_NOFOLLOW`+目录 FD，最终 `fstat` 只接受普通文件。reject `..`、绝对路径、空组件、符号链接、hardlink（st_nlink≠1）、超大文件。复制到父进程专有 `received/`，边复制边计数/SHA-256；此目录不允许解析 sandbox 写入。父进程从实际 PNG header/verify 得到尺寸，验证尺寸/MIME/字节数/hash 与宣称一致，重新应用此次全部图片预算后才返回；accepted/ref 成功才登记。父目录和子目录分离消除“校验后子进程改路径”的窗口。

```python
# ipc.py 的安全目录遍历关键完整函数；调用者持有返回 fd 并负责关闭。
import os
import stat
from pathlib import Path, PurePosixPath
from .contracts import ExtractionError


def open_child_regular(work_dir: Path, relative_path: str) -> int:
    parts = PurePosixPath(relative_path).parts
    if not parts or parts[0] != "child" or any(p in {"", ".", ".."} for p in parts):
        raise ExtractionError("PARSER_OUTPUT_INVALID")
    directory = os.open(work_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        mode = os.fstat(result)
        if not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1:
            os.close(result)
            raise ExtractionError("PARSER_OUTPUT_INVALID")
        return result
    except OSError:
        raise ExtractionError("PARSER_OUTPUT_INVALID") from None
    finally:
        os.close(directory)
```

IPC `asset` 帧只有 LocalAttachment JSON，帧≤64 KiB；不在该帧传正文/数据库 ID/URL。每次只允许一张等待 ACK：父进程 receive→guard→await on_asset→guard→ACK，避免无界任务队列。图片的多个出现位置放完整 manifest，不以事件数量计去重后的图片数。source_path 泄漏禁止；最终安全相对路径仅在 LocalAttachment 中到 P2，不入 manifest/API。

- [ ] **Step 5：只捕获可降级错误。** 格式 Adapter 只 catch `ImageRejected`，产出占位和 warning；生成占位span标为context_prefix，P3不能将其当真实可索引文字；回调异常、权限、租约、数据库/MinIO 失败由父进程原样走编排失败，不包装成 IMAGE_CORRUPT。追加测试：错误 SHA/大小/非PNG、超限新图与重复图、动画首帧、脚本 SVG 拒绝、回调失败不返回完整 result。重跑 Step 2；`git diff --check`。

## P1-T6：Word 顺序/嵌套/Run 空格与 PDF 逐页图片（A05、A07、A18、A29）

**Files:**
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/dify/word_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/dify/pdf_extractor.py`
- Create: `backend/tests/knowledge/test_dify_office_pdf.py`
- Modify: `backend/tests/knowledge/parsing_test_helpers.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/patches.md`

**Consumes:** P1-T1/P1-T3/P1-T5；上游 Word/PDF 源码；现有项目 DOCX 顺序/嵌套能力不能回退。

**Produces:** `WordExtractor/PdfExtractor`；公开纯 fixture `write_pdf(path:Path,pages:list[str]) -> None`（从现有 minimal PDF 生成算法复制到 helper，保留出处，不导入 `_write_pdf`）；PDF 按 page，Word 按有序段落/表格行生成 Document。

- [ ] **Step 1：写真实 DOCX 与 PDF 重点回归。**

```python
# test_dify_office_pdf.py
from docx import Document as WordFile
from actweave_knowledge.extraction.processor import ExtractProcessor
from parsing_test_helpers import make_context, make_setting, write_pdf


def test_word_nested_table_order_repeats_and_run_spaces(tmp_path):
    path = tmp_path / "source.docx"
    document = WordFile()
    document.add_heading("设备", level=1)
    paragraph = document.add_paragraph()
    paragraph.add_run("hello ")
    paragraph.add_run(" world").bold = True
    document.add_paragraph("重复文字")
    outer = document.add_table(rows=1, cols=2)
    outer.cell(0, 0).text = "外层"
    inner = outer.cell(0, 1).add_table(rows=1, cols=1)
    inner.cell(0, 0).text = "嵌套"
    document.add_paragraph("重复文字")
    document.save(path)
    docs = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    text = "\n".join(d.page_content for d in docs)
    assert "hello  world" in text and text.count("重复文字") == 2
    assert text.index("hello") < text.index("外层") < text.index("嵌套") < text.rindex("重复文字")
    assert any("table" in s.location and "row" in s.location for d in docs for s in d.source_spans)
    assert any(d.heading_path == ("设备",) for d in docs)


def test_pdf_keeps_individual_pages_without_string_cache(tmp_path):
    path = tmp_path / "source.pdf"
    write_pdf(path, ["first page", "second page"])
    docs = ExtractProcessor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert len(docs) == 2
    assert [d.source_spans[0].location["page"] for d in docs] == [1, 2]
    assert "first page" in docs[0].page_content and "second page" in docs[1].page_content
```

- [ ] **Step 2：实现纯样例生成器后运行 red。** fixture 不是产品实现；它必须先可运行，不能以缺少测试 helper 作为解析器 red。

```python
# parsing_test_helpers.py 新增；完整可生成多页 PDF，不需要额外下载。
def write_pdf(path: Path, pages: list[str]) -> None:
    entries = [(3 + i * 2, 4 + i * 2, text) for i, text in enumerate(pages)]
    font_id = 3 + len(entries) * 2
    kids = " ".join(f"{page} 0 R" for page, _, _ in entries)
    objects = [(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
               (2, f"<< /Type /Pages /Kids [{kids}] /Count {len(entries)} >>".encode())]
    for page, content, line in entries:
        objects.append((page, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>").encode()))
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects.append((content, b"<< /Length " + str(len(stream)).encode() +
                        b" >>\nstream\n" + stream + b"\nendstream"))
    objects.append((font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    output = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for identifier, body in objects:
        offsets[identifier] = len(output)
        output += f"{identifier} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(output)
    output += f"xref\n0 {len(objects)+1}\n".encode() + b"0000000000 65535 f \n"
    for identifier in sorted(offsets):
        output += f"{offsets[identifier]:010d} 00000 n \n".encode()
    output += (f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n"
               f"{xref_offset}\n%%EOF\n").encode()
    path.write_bytes(output)
```

Run: `env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_dify_office_pdf.py -q`。

- [ ] **Step 3：以局部补丁保持 Word 的全部顺序信息。** `doc.iter_inner_content()` 按 Paragraph/Table 遍历；每个 cell 再 `iter_inner_content()` 递归，不能只取 `.paragraphs`。merged cell 在同一 table 的同一实际 cell 对象只输出一次，但禁止对正文字符串 set 去重。heading style 映射一级到六级标题并更新 heading_path。文档顶层 paragraph 编号按实际 paragraph 出现递增；nested table 用 `table_path` 字符串携带层级，兼容 `table/row/column` 仍为一基。

```python
# word_extractor.py 核心顺序/空格补丁，保留上游 hyperlink/drawing 分支。
from docx.table import Table
from docx.text.paragraph import Paragraph


def ordered_cell_blocks(table: Table):
    seen_cells = set()
    for row_number, row in enumerate(table.rows, 1):
        for column_number, cell in enumerate(row.cells, 1):
            if cell._tc in seen_cells:
                continue
            seen_cells.add(cell._tc)
            for block in cell.iter_inner_content():
                yield row_number, column_number, block


def paragraph_plain_text(paragraph: Paragraph) -> str:
    # 使用者在有 hyperlink/drawing 时按同样的 XML 顺序拼装 Markdown。
    # 不能逐 run.strip()，也不能去重相同文本。
    return "".join(run.text for run in paragraph.runs)
```

超链接继续沿上游字段/relationship 解析，仅保留安全链接或可见文字。远程 relationship 图片不下载；embedded rId 的字节通过 LocalAttachmentSink 接收，按原 paragraph/table 位置插入逻辑 ref。表格有显式重复表头标记时生成 Markdown 短表并记录表头 span；没有实际表头证据时输出列位置字段列表，不能默认首行是标题。每个单元格、原段落、图片分别有 SourceSpan，不给每个文档附全章所有 paragraph。

- [ ] **Step 4：PDF 移植每页提取与图片循环，彻底移除上游字符串缓存。** `PdfDocument`、每页、textpage、image 对象均 finally close；`page_number+1`。图片只有页位置时 alt 为“本页图片”，放页末并用 page+image_index 标识，不声称二维位置；正文空页保留 page Document。读取文字/图片各阶段调用 context.check_cancelled，累加正文时先检查预算再 append。

```python
# pdf_extractor.py 最小文字页实现，图片循环紧接 textpage.close 后写入同一页。
import pypdfium2
from ..base import BaseExtractor
from ..contracts import Document, SourceSpan

class PdfExtractor(BaseExtractor):
    def extract(self, setting, context):
        documents = []
        reader = pypdfium2.PdfDocument(setting.source_path)
        try:
            for number in range(len(reader)):
                context.check_cancelled()
                page = reader[number]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range().replace("\r\n", "\n")
                    finally:
                        textpage.close()
                    documents.append(Document(page_content=text, kind="page",
                        source_spans=(SourceSpan(block_id=f"page:{number+1}",
                            start=0, end=len(text), location={"page":number+1}),)))
                finally:
                    page.close()
        finally:
            reader.close()
        return documents
```

完整 Adapter 保留源 `_extract_images` 的 pypdfium2 image object 提取，但将 UploadFile/storage/session/base_url 替换为 sink；禁止把多个 pages join 到一份私有缓存。纯图片 PDF 的 ExtractionResult 允许承载图片/空文本，以便 P2 完成失败清理；P3 的“可索引文字”判定必须明确 `NO_INDEXABLE_TEXT`，不能因有 Markdown 图片链接就 ready。

- [ ] **Step 5：增加图片页与合并单元格 fixture。** helper 的 `write_pdf` 从已核实的 `test_ingestion.py` 生成算法完整迁入公开函数并转义 `\\`、`(`、`)`；为图片 PDF 使用 pypdf 已有 Image XObject 写入 API 构造小 RGB 图片流，记录预期页数/出现位置；合并单元格测试 assert 一个物理 cell 的内容一次、相同字符串在两个独立段落两次。附带“第二段中间切开”交给 P3-T2 的 span slicing 测试，P1 提供准确逐段输入。重跑 Step 2 和 P1-T5 图像测试；`git diff --check`。

## P1-T7：Unstructured 纯本地适配与离线依赖资源（A02、A06、A08、A24、A25）

**Files:**
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/unstructured_local/__init__.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/unstructured_local/unstructured_pptx_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/unstructured_local/unstructured_epub_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/unstructured_local/unstructured_markdown_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/unstructured_local/unstructured_eml_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/unstructured_local/unstructured_msg_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/unstructured_local/unstructured_xml_extractor.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/unstructured_local/elements.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/runtime_resources.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/resources.lock.json`
- Create: `backend/scripts/build_extraction_resources.py`
- Create: `backend/tests/knowledge/test_local_unstructured.py`
- Create: `backend/tests/knowledge/test_extraction_resources.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/registry.py`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/UPSTREAM.md`
- Modify: `backend/packages/knowledge/actweave_knowledge/extraction/patches.md`

**Consumes:** P1-T2 exact dependency installation；P1-T3 Markdown/HTML safety；P1-T5 images；上游六个本地 Adapter。

**Produces:** 六个具体类遵守 BaseExtractor；`elements_to_documents(elements, *, kind:str) -> list[Document]`；`runtime_manifest() -> dict`、`runtime_digest() -> str`、`probe_parser_resources(parser_id:str) -> str|None`；`build_extraction_resources.py --output PATH` 构建清单入口。资源位于包只读 extraction 资源区域；Tokenizer 文件属于 P3 `ingestion/tokenizer_data`，不进解析 fingerprint。

- [ ] **Step 1：用真实结构元素验证缺少位置也不丢正文、邮件不重复解码。**

```python
# test_local_unstructured.py
from types import SimpleNamespace
from actweave_knowledge.extraction.unstructured_local.elements import elements_to_documents


def test_pptx_missing_page_is_preserved_without_invented_page():
    elements = [
        SimpleNamespace(text="第一页", category="Title", metadata=SimpleNamespace(page_number=1)),
        SimpleNamespace(text="无页码但真实存在", category="NarrativeText", metadata=SimpleNamespace(page_number=None)),
        SimpleNamespace(text="第二页", category="Title", metadata=SimpleNamespace(page_number=2)),
    ]
    docs = elements_to_documents(elements, kind="slide")
    assert "无页码但真实存在" in "\n".join(d.page_content for d in docs)
    unknown = next(d for d in docs if "无页码" in d.page_content)
    assert all("slide" not in s.location for s in unknown.source_spans)
    assert any(w.code == "SOURCE_POSITION_UNAVAILABLE" for w in unknown.warnings)


def test_email_element_is_already_decoded():
    text = "SGVsbG8= 是用户写下的字面值；中文正文。"
    elements = [SimpleNamespace(text=text, category="NarrativeText", metadata=SimpleNamespace())]
    docs = elements_to_documents(elements, kind="mail")
    assert docs[0].page_content == text
```

- [ ] **Step 2：运行 red。** `env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_local_unstructured.py tests/knowledge/test_extraction_resources.py -q`；resource 测试文件先含 Step 5 代码。不接受 missing Unstructured 的 import error 当功能 red；依赖准备按 P1-T2 单独执行。

- [ ] **Step 3：移植时只保留本地分支，删除 API 参数、配置 import、运行时下载和隐含解码。**

| Adapter | 唯一允许 partition 入口 | 本地修正 |
| --- | --- | --- |
| UnstructuredPPTXExtractor | `unstructured.partition.pptx.partition_pptx(filename=...)` | 实际 page_number→slide；无页码原位保留，不丢不猜 |
| UnstructuredEpubExtractor | `unstructured.partition.epub.partition_epub(filename=...)` | Pandoc 仅使用已安装 binary；移除 download_pandoc；源章节能确认才填 chapter |
| UnstructuredMarkdownExtractor | `unstructured.partition.md.partition_md(filename=...)` | 先保护代码/字面泛型 token 再调用，恢复原文与 heading_path；MDX 不执行 |
| UnstructuredEmlExtractor | `unstructured.partition.email.partition_email(filename=...)` | 删除 base64.b64decode 二次猜测；保留真实正文 |
| UnstructuredMsgExtractor | `unstructured.partition.msg.partition_msg(filename=...)` | 不请求远程邮件/图片；MSG fixture 可读 |
| UnstructuredXmlExtractor | `unstructured.partition.xml.partition_xml(filename=...)` | 预先拒绝 DTD/entity，安全序列化后的本地 XML 才传库 |

```python
# elements.py：最小不丢元素实现；表格/标题分支随后按真实category增强。
from ..contracts import Document, ParseWarning, SourceSpan


def elements_to_documents(elements, *, kind: str) -> list[Document]:
    documents = []
    for index, element in enumerate(elements, 1):
        text = element.text or ""
        metadata = element.metadata
        page = getattr(metadata, "page_number", None)
        location = {"element": index}
        warnings = ()
        if isinstance(page, int) and page > 0:
            location["slide" if kind == "slide" else "page"] = page
        elif kind == "slide":
            warnings = (ParseWarning(code="SOURCE_POSITION_UNAVAILABLE",
                message="解析库未提供页码", source_position=location),)
        documents.append(Document(page_content=text, kind=kind, warnings=warnings,
            source_spans=(SourceSpan(block_id=f"{kind}:element:{index}", start=0,
                                    end=len(text), location=location),)))
    return documents
```

完整转换保留有序元素，不先转 dict by page 导致无 page 元素丢失；只相邻且实际位置相同才合并。Title 更新 heading_path；Table 的 `metadata.text_as_html` 调用 P1-T3 `html_to_documents`，位置沿元素真实 metadata；只有纯 text 的 table 给 `TABLE_STRUCTURE_UNAVAILABLE` warning，不制造表头。粗分段用固定字符预算只限制第一阶段；任何 `max_characters` 不能宣称 Token。

Markdown 保护以 `markdown-it-py==4.2.0` 已锁定解析 token.map 为源行映射，提取 fenced/code_inline 和普通文本中的泛型/占位符原文，换成不能与源文件碰撞的确定 marker，partition 后恢复；不对原文中 `<script>` 当成需要执行的 HTML。必须让 local 模式通过 P1-T3 同组 literal assertions；保留源行 SourceSpan 而非用 Unstructured 缺失行号猜测。

XML 用 `lxml.etree.XMLParser(resolve_entities=False,load_dtd=False,no_network=True)` 预读并检查 docinfo.doctype；任何 DTD/实体声明直接拒绝 `FORMAT_SIGNATURE_MISMATCH`，防止随后 partition 自己换解析器展开实体。输入字节先识别 XML 自带编码，不能只扫描 UTF-8 字符串漏掉 UTF-16 DTD。禁网隔离是 P1-T8 的额外边界，不取代 XML 自身限制。

- [ ] **Step 4：按锁定 wheel 源码核查资源/遥测，不编造环境变量。** 安装时用以下只读命令定位真实库，检查下载、HTTP、模型加载调用，并把确切文件/符号和采用的禁用方式写入 resources 清单说明。

```bash
.venv/bin/python - <<'PY'
from importlib.metadata import distribution
for name in ('unstructured', 'spacy', 'pypandoc-binary', 'python-magic'):
    dist = distribution(name)
    print(name, dist.version)
    for item in dist.files or ():
        if str(item).endswith('.py') and any(word in str(item) for word in ('nlp','telemetry','partition','pandoc')):
            print(dist.locate_file(item))
PY
```

先确认实际选用 NLP 分支及加载器；在安装/镜像构建阶段准备其请求的模型/字典，以固定 wheel/资源 SHA 登记。Pandoc 从 pypandoc-binary 所带文件定位并校验 SHA；libmagic、codec 以系统包版本及探测输出记录，不能向终端/响应打印宿主私有路径。遥测若可通过锁定源码的公开开关禁用则设置并测试；若仍有 import-time 或运行时网络请求，则对该确切调用做本地最小禁用补丁并记录，不通过 monkey patch requests 充当生产安全边界。

缺少任何必需资源时 probe 返回 `PARSER_DEPENDENCY_UNAVAILABLE`；启动能力清单明确格式不可用，不在 parse 中下载。生产 Dify 模式包含 PPTX/EPUB，故其必要 Pandoc/NLP 资源也必须安装。若当前目标平台无法满足资源安装，记录失败并停止宣称该模式 ready；不得改路由到 API/旧解析器。

- [ ] **Step 5：实现可复现资源清单并做篡改测试。** manifest 只记录解析库包名/版本、所需文件的包相对逻辑名/SHA、adapter 修订和实际禁网策略版本；不含绝对路径、构建机器用户名、timestamp、Tokenizer/ChunkProfile，防止不同构建时间无意义失效。

```python
# test_extraction_resources.py
from actweave_knowledge.extraction.runtime_resources import runtime_manifest, runtime_digest


def test_runtime_manifest_is_stable_safe_and_not_chunk_dependent():
    manifest = runtime_manifest()
    assert runtime_digest() == runtime_digest()
    assert manifest["format_version"] == 1
    names = {item["name"] for item in manifest["packages"]}
    assert "unstructured" in names and "pypdfium2" in names
    assert "tiktoken" not in names
    assert all(not item["logical_name"].startswith("/") for item in manifest["resources"])
    assert all(len(item["sha256"]) == 64 for item in manifest["resources"])
```

`build_extraction_resources.py` 读取 explicit allowlist 中所需包的 `distribution(name).version` 和实际资源文件 bytes，排序后写 canonical JSON；这些资源路径来自 Step 4 已审查清单，不遍历整个虚拟环境。`runtime_manifest` 启动时重新验 SHA，与 checked-in platform 分项比对；Mac/Linux 原生 binary 允许不同分项，但同一部署 Gateway/Worker 必须消费同一平台分项。`runtime_digest` 为 canonical JSON 的 SHA-256，`extractor_version` 采用 `dify:<full-commit>:adapter-v1:<runtime-digest>`，Unstructured 类同。仅更换 Tokenizer 不改变此值；换解析/NLP/图像资源必须改变。

- [ ] **Step 6：真实格式矩阵 green。** 使用 Step 1 元素单测之外的真实 `.pptx/.epub/.md/.eml/.msg/.xml` 文件，逐项断言文本/位置/警告；PPTX/EPUB 分别跑 dify 与 local；EML 构造带 UTF-8 encoded MIME 及字面 Base64 的正文；XML 用本地与 HTTP 外部实体两种恶意输入。MSG 使用锁定 python-oxmsg 上游测试库中的小 fixture，固定其 bytes SHA/出处并检查正文，不将假的 OLE header 算 MSG 通过。fixture 导入是实施阶段下载，运行时不下载。重跑 Step 2；记录资源安装和格式通过分别的结果。

## P1-T8：受控子进程、取消结算与禁网矩阵（A12、A18、A23、A24、A25）

**Files:**
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/runtime.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/child.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/sandbox.py`
- Create: `backend/packages/knowledge/actweave_knowledge/extraction/sandbox-macos.sb`
- Create: `backend/tests/knowledge/test_extraction_runtime.py`
- Create: `backend/tests/knowledge/test_extraction_offline_matrix.py`
- Create: `backend/tests/knowledge/fixtures/parser_child_hang.py`
- Create: `backend/scripts/check_extraction_runtime.py`
- Modify: `README.md`（仅补新解析包的离线准备/未接生产状态）
- Modify: `backend/AGENTS.md`（仅补 extraction 边界和定向门禁）

**Consumes:** P1-T1–T7；总计划 `run_extraction` 签名；父级 guard/on_asset；工作目录由 caller 创建并 finally 清理。

**Produces:**

```text
async run_extraction(setting: ExtractSetting, *, work_dir: Path,
    limits: ExtractionLimits, timeout_seconds: int,
    on_asset: Callable[[LocalAttachment], Awaitable[None]],
    guard: Callable[[], Awaitable[None]]) -> ExtractionResult
```

另产出 `ParserSlots(capacity:int)`（async context manager，无等待队列）、`sandbox_command(command:list[str],*,work_dir:Path)->list[str]`、`parser_environment(work_dir:Path)->dict[str,str]`、`stop_process_group(process:asyncio.subprocess.Process)->None`、`check_extraction_runtime.py --matrix --output PATH`。Gateway 由 P3 创建进程级 `ParserSlots(1)`；Worker 使用既有并发默认 2 的任务槽，不根据 timeout 值猜当前角色，不创建第二个全局并发体系。

- [ ] **Step 1：先写真正会阻塞的子进程 fixture 和回收测试。** 注入的是测试 `sandbox_command` 输出，不是新增生产任意 executable 输入；正式生产只能运行固定 child module。

```python
# fixtures/parser_child_hang.py
import os
import signal
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    signal.pause()
```

```python
# test_extraction_runtime.py
import os
import sys
from pathlib import Path
import pytest
from actweave_knowledge.extraction import runtime
from actweave_knowledge.extraction.contracts import ExtractionError, ExtractionLimits
from parsing_test_helpers import make_setting

@pytest.mark.asyncio
async def test_timeout_reaps_child_before_return(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"; source.write_text("hello", encoding="utf-8")
    pidfile = tmp_path / "child.pid"
    fixture = Path(__file__).parent / "fixtures" / "parser_child_hang.py"
    monkeypatch.setattr(runtime, "sandbox_command",
        lambda command, *, work_dir: [sys.executable, str(fixture), str(pidfile)])
    async def guard():
        return None
    async def on_asset(asset):
        raise AssertionError("fixture cannot produce an asset")
    with pytest.raises(ExtractionError) as caught:
        await runtime.run_extraction(make_setting(source), work_dir=tmp_path / "work",
            limits=ExtractionLimits(), timeout_seconds=1, guard=guard, on_asset=on_asset)
    assert caught.value.reason_code == "PARSER_TIMEOUT"
    pid = int(pidfile.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

@pytest.mark.asyncio
async def test_parser_slots_reject_busy_without_queueing():
    slots = runtime.ParserSlots(1)
    async with slots:
        with pytest.raises(ExtractionError) as caught:
            async with slots:
                raise AssertionError("second parser must not start")
    assert caught.value.reason_code == "PARSER_BUSY"
```

- [ ] **Step 2：运行 red。** `env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_extraction_runtime.py -q`。

- [ ] **Step 3：实现 launcher、严格 wire 协议与有界接收。** parent 仅把 ExtractSetting/ExtractionLimits 的 JSON 发 stdin（受50 MiB源文件准入保护，不传正文）；source_path 在 sandbox 只读挂载/规则内。child 重建 context/registry/LocalAttachmentSink，读取固定输入，发 asset/result/error JSON 帧到专用 pipe；库 stdout/stderr 不当协议，stderr 限长且仅映射安全 code，不写源文件内容或原生 stack 到公开错误。

child 为每张规范图发送 asset 帧并等 ACK，父进程 receive_asset→guard→on_asset→guard 后才 ACK。final result 序列化为 child/manifest.json，父进程从安全 FD 读≤50 MiB、decode_manifest、校验 final 清单恰好等于已接受 ref 集合；source_sha256 必须重新计算和输入一致。缺失末帧、额外对象、未知消息类型、重复完成、损坏帧均 `PARSER_OUTPUT_INVALID`；frame 长度上限64 KiB，固定 `readuntil` limit，无无限缓冲。JSON 不允许 pickle 或 Python import 指令。

```python
# runtime.py 最小 admission 与回收代码
import asyncio
import os
import signal
from .contracts import ExtractionError

class ParserSlots:
    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.active = 0

    async def __aenter__(self):
        # 同一事件循环中 check+increment 无 await，满额立即可重试失败。
        if self.active >= self.capacity:
            raise ExtractionError("PARSER_BUSY", "解析繁忙，请稍后重试")
        self.active += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.active -= 1


async def stop_process_group(process: asyncio.subprocess.Process) -> None:
    # start_new_session=True 保证 pid 同时是本次解析进程组 ID。
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    finally:
        # group leader 已退出仍可能遗留 Pandoc 后代，继续终止该解析组。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
```

`run_extraction` 使用 `asyncio.create_subprocess_exec(*sandbox_command(...), start_new_session=True, env=parser_environment(...))`，不在 event loop 做同步文件遍历。timeout_seconds 由宿主传预览120或任务剩余预算（总900包含下载/模型），不能解析完成后才开始计时。timeout/cancel/guard失败：停止接受新资产→shield 回收进程组→等待已发出的 on_asset 完成/失败结算→向上抛原异常。保留 pending on_asset Task 的强引用；取消不取消底层已开始 PUT；此线程/对象I/O语义由 P2 adapter 保证。run_extraction 不擅自删除 caller 工作目录，返回/抛异常时保证无解析进程继续写，caller 才 finally 清理。

work_dir 包含 `source`、child 输出和 parent received；输入本地 source 如果在目录之外，以受限复制进入 source 子目录，复制字节也计入512 MiB；不得重复将同一 source 同时计两遍。父进程在读asset/manifest前检查目录字节，加上运行时每≤1秒的有限文件统计检查；超限立即终止，ZIP声明/正文/图片预算仍在各加载点提前限制。并发临时空间按宿主槽数预留，此检查不承诺严格 RSS 或瞬时磁盘峰值。

- [ ] **Step 4：以操作系统边界禁网并削减文件/凭据可见性。** parser_environment 使用白名单：locale、只读 Python/package 运行路径、任务 TMPDIR、已审查资源位置与遥测禁用设置；不继承数据库、MinIO、代理、云、模型 Key 环境变量。不得简单 `os.environ.copy()` 再删几个已知 key。固定子进程只读其 Python 标准库/虚拟环境/解析资源及原件，仅可写 child/，不能写 parent received/或任意宿主 home。

Linux 使用可用且经部署验证的 bubblewrap，至少 `--unshare-net --unshare-pid --die-with-parent --proc /proc --dev /dev`，对 Python/运行库/资源做只读 bind，对 child 目录做唯一 writable bind；不 `--ro-bind / /` 暴露宿主所有秘密。Mac 使用系统 `sandbox-exec` 和 checked-in deny-default profile，明确允许解释器/动态库读取、child 文件写入、必要进程 syscall，显式 deny network。profile 路径参数仅由宿主 Path 生成，不接受用户自由规则。若平台不支持或权限禁用相应 sandbox，probe 返回不可用，新解析流程 fail closed；不能悄悄裸跑 Python。测试机 mock 网络仅是补充检查。

安装 bubblewrap 或系统 Pandoc/libmagic 属于实施环境准备，P4 镜像计划纳入相同版本/规则；如生产基础镜像无法支持 network namespace，需要先解决部署隔离条件，不能用“当前只调用本地函数”当等价结论。`sandbox_command` 必須用 argv，不 shell=True；Mac profile 中路径通过 sandbox 参数传递，不字符串拼接未转义文档名。

- [ ] **Step 5：离线矩阵是真实样例，不连数据库/模型。** `check_extraction_runtime.py` 调用固定 registry+run_extraction，on_asset 只在父目录读取/计数，guard 是 async no-op；每项比较预期内容、来源、附件 ref、warning。对 TXT/MD/PDF/DOCX/XLSX/XLS/CSV/HTML/HTM/PPTX/EPUB 和 local-only EML/MSG/XML 建固定小 fixture；所有别名扩展名经过对应签名检查；每项 JSON 记录 mode/extension/parser_version/result/counts，禁止正文/路径入诊断输出。执行：

```bash
env -u DATABASE_URL .venv/bin/python scripts/check_extraction_runtime.py --matrix --output /tmp/knowledge-extraction-offline.json
env -u DATABASE_URL .venv/bin/python -m pytest tests/knowledge/test_extraction_offline_matrix.py -q
```

禁网证明必须包含一个在相同 sandbox 里主动尝试 socket connect 的测试子进程：期待 OS 拒绝且本地监听服务 accept 计数为0，不能用不存在域名的 DNS 失败代替。再在已禁止外部网络的 Linux 验证容器执行矩阵，启动时已具全部 wheels/Pandoc/NLP资源，不挂载网络凭据。分别移除 Pandoc/NLP资源校验副本，期待 capability unavailable，不能尝试公网下载；资源篡改期待摘要不匹配。

取消/失败验收以可 await 的 Event/barrier 控制：on_asset 开始后撤 guard、阻塞回调时 cancel、child 完成前异常、final manifest 缺图、磁盘预算超限、子进程生成后代后退出。所有 case assert 回收先于目录删除，已开始回调结算后才返回；不靠 sleep 构造竞争。增加“含图片但无实际文字”结果交 P3判定的测试，绝不调用 OCR/模型补文字。

- [ ] **Step 6：P1 交付验收和文档。** 在 Python 3.12/macOS、目标 Linux 分别运行全部新增 P1 测试及真实格式矩阵，记录 passed/failed/skipped 和资源 manifest；任何 skip 要标为未验，不写全通过。执行 `make format`、`make lint` 和 `git diff --check`（backend 所有受影响文件）；全 backend `make test` 涉及 PostgreSQL，由总计划后续集成门禁另行执行，P1 离线结果不替代它。README/后端指南只说明新 extraction 包尚未接生产、资源安装和禁网前提、默认格式矩阵及限制，不提前宣称上传UI已变更。

## 交付给 P2/P3/P4 的验收映射

| 规格 | P1 证据 | 后续责任 |
| --- | --- | --- |
| A01/A02 | P1-T1/T2 的类型、来源、唯一登记与拒绝矩阵 | P3 能力HTTP与准入同源 |
| A03/A04/A05/A06/A07/A08 | P1-T3/T4/T6/T7 的格式内容、来源和警告 | P3切分后不破坏、P4展示 |
| A09/A29 | P1-T4/T6 原行/原段 SourceSpan 和字段绑定 | P3实际切片+context_prefix偏移 |
| A11/A14 | P1-T1/T3 稳定manifest/parse fingerprint | P2完整缓存；P3 index_text |
| A12/A15/A18 | P1-T5/T8 安全ref、可降级错误边界、无对象I/O | P2资产登记；P3无副作用预览 |
| A23/A24/A25 | P1-T1/T5/T7/T8 限额、回收、离线、资源一致 | P4生产镜像/宿主边界验证 |

本文件是未来执行步骤；编写计划只核对了现有源码、现有依赖元数据和测试入口，没有安装候选依赖，没有运行新增测试、调用数据库/模型或验证目标部署。
