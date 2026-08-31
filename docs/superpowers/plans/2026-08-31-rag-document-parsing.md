# RAG 文件解析重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将知识库切换到 Dify 解析器移植、本地 Unstructured、可追踪 Markdown、图片资产和真实 Token 切分，并通过端到端与离线门禁。

**Architecture:** 本计划是四个交付包的总入口。Extraction 只处理已授权的本地文件，宿主控制数据库、MinIO、配额和模型调用；不可变提取结果同时承担附件归属和解析缓存。预览与正式入库使用同一规范化/切分流程，只有附件落地方式不同。

**Tech Stack:** Python 3.12、SQLAlchemy/asyncpg/PostgreSQL/pgvector、MinIO、Dify 固定版本解析器、Unstructured 本地库、tiktoken、Next.js/React/TypeScript、pytest/Rstest/Playwright。

**Spec:** [RAG 文件解析重构规格](../specs/2026-08-31-rag-document-parsing-design.md)。各分计划都必须与规格和本总计划一起阅读。

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

## 1. 范围状态与实施前置

用户已要求生成执行计划。本计划按现有规格执行；前面讨论过的可选多模态 OCR 尚未写入规格，因此本核心计划不悄悄实现 OCR。已向用户询问是否纳入；若要求纳入，应先补充 OCR 模型、缓存和预览一致性契约，再加独立任务，不改写现有纯本地解析约束。

当前 M11 仅部分 schema/契约存在，`app/knowledge/config.py` 仍读 YAML，`RegistryKnowledgeModelPort.generate_summary` 仍有未实现分支。不得把 M11 文档状态当作完成证据。相关任务见 [M11 计划归档](backup/2026-08-31-rag-knowledge-m11.md)。

- P1 纯解析、数据类型、离线资源与样例测试可基于当前代码开展，不接通生产路径。
- P2/P3/P4 接入前先完成 M11 既有计划中实际需要的 PostgreSQL 配置读取、摘要生命周期与管理配置界面，并记录新基线；不在本计划重新实施一套 M11。
- 若 M11 函数名或结构发生变化，更新本计划中的宿主接入位置及测试后再实施；不是在运行时动态猜测可用入口。
- D01 按严格本地解析执行；API 服务不会作为“依赖缺失时的自动降级”。

## 2. 分计划与依赖

| 顺序 | 分计划 | 交付 | 执行条件 |
| --- | --- | --- | --- |
| P1 | [解析模块与本地格式](2026-08-31-rag-document-parsing-p1-extraction.md) | 稳定接口、移植解析器、结构/编码修正、本地资源与进程运行器 | 可先独立推进 |
| P2 | [附件、缓存与配额](2026-08-31-rag-document-parsing-p2-storage.md) | schema、配额、登记/写入/删除、缓存和授权读取 | P1 类型固定；M11 宿主基线完成 |
| P3 | [切分与摄取接入](2026-08-31-rag-document-parsing-p3-ingestion.md) | Token/结构切分、配置快照、预览、发布、重处理和检索一致性 | P1+P2 |
| P4 | [前端与最终门禁](2026-08-31-rag-document-parsing-p4-frontend.md) | 动态格式、Markdown/图片、表头和单位、浏览器/离线/质量门 | P3 DTO 固定 |

不得四个包同时修改公共 contracts、pipeline 或 schema；解析器内部可以在注册表/接口固定后并行，但最终接入和发布检查顺序执行。每个分计划开始时记录输入版本，结束时记录真实命令与结果。

## 3. 共用目标类型：所有分计划必须采用同一命名

以下是要创建的代码接口，不是当前仓库已有符号。类型拥有者为 `backend/packages/knowledge/actweave_knowledge/extraction/contracts.py`，P1-T1 创建并导出。

| 类型 | 字段/语义 |
| --- | --- |
| `SourceSpan` | `block_id: str, start: int, end: int, location: dict[str, str | int], role: Literal['source','context_prefix']='source'`；偏移为当前规范化字符串字符下标 |
| `ParseWarning` | `code: str, message: str, source_position: dict[str, str | int]`；无路径/正文/秘密 |
| `HeaderRule` | `sheet: str | None, mode: Literal['auto','none','explicit'], row: int | None`；行号从 1 起，explicit 要求 row |
| `ParseProfile` | `etl_type, extractor_id, extractor_version, normalization_version, image_policy_version: str; header_rules: tuple[HeaderRule, ...]`；只含解析阶段影响因素 |
| `ChunkProfile` | `unit: Literal['character','token'], mode: Literal['general','parent_child'], size:int, overlap:int, separator:str, child_size:int, child_separator:str, remove_extra_spaces:bool, remove_urls_emails:bool, tokenizer_profile_id:str|None, tokenizer_digest:str|None, cleaner_version:str, splitter_version:str` |
| `ProcessingProfile` | `parse: ParseProfile, chunk: ChunkProfile`；数据库文档的 `parsing_profile` 保存该结构，不把 chunk 参数混入提取缓存键 |
| `Attachment` | `ref:str, media_type:str, size_bytes:int, width:int, height:int`；ref 为安全规范化字节 SHA-256，无数据库 ID 或路径 |
| `LocalAttachment` | `attachment: Attachment, relative_path:str`；只在子进程 IPC/父进程消费期间存在，不进入 manifest/API |
| `AttachmentOccurrence` | `ref:str, alt_text:str, source:SourceSpan`；同图多位置不合并 |
| `Document` | `page_content:str, source_spans:tuple[SourceSpan,...], heading_path:tuple[str,...], kind:str, attachments:tuple[AttachmentOccurrence,...], warnings:tuple[ParseWarning,...]`；`metadata` 兼容投影仅含这些安全结构 |
| `ExtractionResult` | `documents:tuple[Document,...], attachments:tuple[Attachment,...], warnings:tuple[ParseWarning,...], source_sha256:str, parse_fingerprint:str`；可稳定序列化，不含临时路径 |
| `ExtractSetting` | `source_path:Path, original_name:str, datasource_type:Literal['file'], profile:ParseProfile`；只传已经准入的本地源文件 |
| `ExtractionLimits` | 按 Global Constraints 定义正文、图片、manifest、工作目录上限；从固定常量构造，拒绝非正数 |
| `ExtractionContext` | `work_dir:Path, sink:AttachmentSink, limits:ExtractionLimits, check_cancelled:Callable[[],None]`；无宿主会话/凭据 |

所有公开数据模型使用 frozen Pydantic model、`extra='forbid'`；带 sink/callback 的内部 ExtractionContext 另启用 `arbitrary_types_allowed=True`，不序列化到 manifest。路径对象仅出现在内部输入。禁止从 metadata 读取项目权限。所有集合默认空 tuple，不能共享可变默认值。

`extractor_version` 包含固定 Dify commit、Adapter 修订及实际解析依赖/资源摘要，不含 Tokenizer/切分器摘要。文本编码保存在 `SourceSpan.location['encoding']`，前端不能将其解释为页号。

### 3.1 解析与序列化 Interface（P1 产出）

```text
BaseExtractor.extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]
AttachmentSink.accept(self, source_path: Path, *, alt_text: str, source: SourceSpan) -> Attachment
ExtractorRegistry.resolve(self, *, datasource_type: str, etl_type: str, extension: str) -> ExtractorRegistration
ExtractProcessor.extract(self, setting: ExtractSetting, context: ExtractionContext) -> list[Document]
canonical_parse_fingerprint(profile: ParseProfile) -> str
encode_manifest(result: ExtractionResult) -> bytes
decode_manifest(payload: bytes, limits: ExtractionLimits) -> ExtractionResult
normalize_documents(documents: list[Document]) -> list[Document]
async run_extraction(setting: ExtractSetting, *, work_dir: Path, limits: ExtractionLimits,
                     timeout_seconds: int, on_asset: Callable[[LocalAttachment], Awaitable[None]],
                     guard: Callable[[], Awaitable[None]]) -> ExtractionResult
```

`ExtractorRegistration`（P1 定义）有 `extractor_id, extractor_version, extensions, etl_types, supports_embedded_images, factory, dependency_probe`。factory 延迟导入格式库，dependency_probe 返回安全 reason_code，不返回本机路径。

P1 另提供 `ParserSlots(capacity: int)`：非排队的异步上下文管理器；Gateway 在进程级持有 `ParserSlots(1)` 并包围每次预览解析，满槽立即返回资源忙。Worker 继续由已有任务并发控制，不再叠加另一组默认并发配置。XLS/XLSX 分开注册图片能力，可以共用解析实现。

### 3.2 存储 Interface（P2 产出）

类型拥有者 `storage/extractions.py`：`StoredExtraction(extraction_id:UUID, document_id:UUID, result:ExtractionResult)`；`ExtractionReservation(extraction_id:UUID, document_id:UUID, project_id:UUID, base_id:UUID, task_id:UUID, attempt:int)`。已存在 `KnowledgeTaskClaim` 继续使用，不发明第二个 claim。

```text
async ExtractionStore.find_ready(claim: KnowledgeTaskClaim, *, source_sha256: str,
                                 profile: ParseProfile, limits: ExtractionLimits) -> StoredExtraction | None
async ExtractionStore.begin(claim: KnowledgeTaskClaim, *, source_sha256: str,
                            profile: ParseProfile) -> ExtractionReservation
async ExtractionStore.persist_attachment(reservation: ExtractionReservation,
                                         asset: LocalAttachment, *, work_dir: Path) -> None
async ExtractionStore.complete(reservation: ExtractionReservation,
                               result: ExtractionResult) -> StoredExtraction
async ExtractionStore.enqueue_cleanup(extraction_id: UUID, *, project_id: UUID) -> None
async KnowledgeStorageQuotaPort.reserve(session: AsyncSession, *, project_id: UUID,
                                        object_id: UUID, size_bytes: int) -> None
async KnowledgeStorageQuotaPort.commit(session: AsyncSession, *, object_id: UUID) -> None
async KnowledgeStorageQuotaPort.release(session: AsyncSession, *, object_id: UUID) -> None
```

Store 在每次外部 I/O 前后检查保存的 claim 与文档状态。配额 session 由同一次对象登记事务传入，不在持有事务时 PUT。manifest 的对象计量 ID 使用 extraction_id，原文件使用 document_id，附件使用 attachment_id。

P2 统一拥有本期 schema：Document 的 `source_sha256/parsing_profile/published_extraction_id`，Segment/Child 的 `index_text/token_count/source_spans`，Segment 的 `extraction_id`，Task 的 `extraction_id` pin 以及提取结果/附件/绑定实体。缓存读取必须先持久 pin 再做 I/O；任务结算或失效后清除本 claim 的 pin。P3 发布必须同时写 Segment.extraction_id、绑定和 Document.published_extraction_id，满足 P2 的延迟一致性约束。

冻结配置沿用现有承载位置：首次上传将 ProcessingProfile 存入 Document.parsing_profile；显式reparse在已有 Task.reparse_settings 内新增 `processing_profile` 与 `capability_revision`，原chunk字段保留为严格一致的兼容投影。普通上传任务的 reparse_settings 仍为NULL；不新增通用任务payload。重试读取同一冻结值，reparse成功才替换Document配置。

配额 reserve 增加 reserved；PUT 确认后 commit 把相同字节从 reserved 移到 used，总量不变；确认对象删除后 release 扣对应轴。校准保留这两轴和现有 PrivateFile/Project Skill 用量，不能重复扣费或清零其它资产。

### 3.3 切分与预览 Interface（P3 产出）

```text
count_knowledge_tokens(text: str, *, profile_id: str = 'knowledge-cl100k-v1') -> int
build_index_text(markdown: str) -> str
split_documents(documents: tuple[Document, ...], *, profile: ChunkProfile) -> list[SegmentDraft]
preview_fingerprint(*, source_sha256: str, extension: str,
                    profile: ProcessingProfile, capability_revision: str) -> str
```

沿用 `ingestion/splitter.py::SegmentDraft` 并扩展，构造字段统一：`position:int, content:str, index_text:str, token_count:int, source_position:dict[str,str|int], source_spans:tuple[SourceSpan,...], attachments:tuple[AttachmentOccurrence,...], children:tuple[ChildDraft,...]`；`ChildDraft(content:str,index_text:str,token_count:int,source_spans:tuple[SourceSpan,...])`。旧字符串 children 的兼容适配仅放在旧 character profile 调用入口，不能同时把同一字段当两种类型。

### 3.4 HTTP DTO（P3 定义，P4 消费）

`GET /api/projects/{project_id}/knowledge/file-capabilities`：

```json
{
  "effective_etl": "dify",
  "capability_revision": "sha256-of-runtime-manifest",
  "formats": [{"extension": ".pdf", "parser_id": "dify.pdf", "available": true,
               "reason_code": null, "embedded_images": true}],
  "chunk_limits": {"unit": "token", "tokenizer_profile_id": "knowledge-cl100k-v1",
                   "parent_min": 200, "parent_max": 4000, "parent_max_chars": 4000,
                   "overlap_max": 500, "child_min": 100, "child_max": 2000}
}
```

这里的 revision 示例仅说明格式，实际值由本地依赖/资源/解析器 manifest 计算，不能硬编码该示例字符串。

现有预览响应保留 `total` 与 `chunks`；每个 chunk 保留 position/content/word_count/child_contents，并新增 token_count/source_spans/attachments（逻辑ref与alt，不含ID/URL）。顶层新增 `preview_fingerprint, source_sha256, effective_profile, warnings, preview_attachments, omitted_preview_attachment_count, table_sources`。`preview_attachments` 元素为 `ref, media_type, data_base64`，只允许安全缩略图类型；前端 Blob URL 不写回 API。`table_sources` 元素为 `sheet:str|None, header_mode:Literal['auto','none','explicit'], header_row:int|None, header_cells:list[str]`，仅为 CSV/Excel 的服务器表头诊断；无表格时为空列表，行号从 1 起。header_row 是当前auto候选或explicit选定的原始行号；none为null。header_cells保留原始表头值，来源为table_header的逐列source spans；它由解析结果投影，不由浏览器重新解析文件。

新增上传/重解析参数：`processing_profile` 的用户可配字段（单位、分段参数、header_rules），可选 `expected_preview_fingerprint`。parser/model/storage 身份由服务器覆盖，不能接受客户端的任意 extractor_version 或对象位置。原有 chunk_size 等表单字段由 Gateway 统一映射，不提供两套冲突参数同时生效。

Document DTO 新增安全 `parsing_profile`（ProcessingProfile）、`parse_warnings`；Segment 详情新增 token_count/source_spans/attachments，附件元素为 `attachment_id, ref, alt_text, media_type, width, height`。API 详情不得返回 index_text 的内部控制字段、storage_key 或工作目录。

管理附件读取路径：`/api/projects/{project_id}/knowledge/documents/{document_id}/segments/{segment_id}/attachments/{attachment_id}`；引用附件读取路径多一段 `/bases/{base_id}`。两者要求 `expected_document_version` 与 `expected_content_digest`，使用对应现有管理/引用授权逻辑；不是由 query flag 开启管理绕过。

## 4. 共用测试约定

- P1-T1 创建 `backend/tests/knowledge/parsing_test_helpers.py`，公开 `make_parse_profile(extension, *, etl_type='dify', header_rules=())`、`make_chunk_profile(**overrides)`、`make_document(text, *, location=None, heading_path=())`、`make_setting(path, **overrides)`、`CollectingAttachmentSink(work_dir:Path)`、`make_context(work_dir:Path)`、`write_pdf(path,pages)`。P3-T4 再添加 `write_docx_with_image(path)`。后续计划引用这些 helpers，不私自定义另一套 profile 字段。
- P2 创建 `backend/tests/knowledge/extraction_test_helpers.py`，公开异步上下文管理器 `extraction_harness(postgres_database_url, *, quota_bytes=524288000)`。yield 对象必须有 `store, object_store, quota, session_factory, claim, project_id, base_id, document_id, read_rows(), published_result()`；故障点用 `object_store.fail_next(operation)` 和可 await 的 barrier，不能靠 sleep 制造竞态。
- P3 创建 `backend/tests/knowledge/ingestion_test_helpers.py`，公开 `ingestion_harness(postgres_database_url, *, etl_type='dify', cache_enabled=True)`，复用 P2 harness，新增 `upload(path, profile), preview(path, profile), run_next_task(), segments(document_id), reparse(document_id, profile), reembed(base_id)`。记录模型输入供 assertions 使用。
- 新 helpers 不从其它测试模块导入 `_private` fixture。现有 `test_ingestion.py` 的文件生成器迁到 parsing_test_helpers 时，同步修改原测试引用，保持原门禁覆盖。
- 纯解析测试使用 `env -u DATABASE_URL .venv/bin/python -m pytest ...`，不会连接开发数据库；数据库测试通过现有 core_gate_plugin 和 postgres fixtures 创建随机测试库，不能直接连业务库跑 DDL。

## 5. 覆盖与交付记录

以下映射固定本期验收责任；实施时在对应任务下记录实际测试 node ID 和结果，不能把“全量测试”当作逐项需求证明。

| 验收项 | 实现与验证任务 |
| --- | --- |
| A01 路由 | P1-T2；P3-T3；P4-T1/T2 |
| A02 隔离与来源 | P1-T1/T2/T7 |
| A03 Excel | P1-T4；P4-T2 |
| A04 CSV与编码 | P1-T3/T4 |
| A05 Word | P1-T6 |
| A06 Markdown | P1-T3/T7；P3-T2 |
| A07 PDF | P1-T5/T6；P3-T1/T5 |
| A08 长尾格式 | P1-T7 |
| A09 表格切分 | P1-T4；P3-T2 |
| A10 Token | P3-T1/T2 |
| A11 内容派生 | P1-T3；P3-T1/T2/T5/T6 |
| A12 预览一致 | P1-T8；P3-T4/T5；P4-T3/T5 |
| A13 前端竞态 | P3-T3/T4；P4-T2/T3 |
| A14 完整缓存 | P1-T1；P2-T4；P3-T5 |
| A15 幂等 | P1-T5；P2-T1/T3/T4/T5；P3-T5 |
| A16 原子发布 | P2-T1/T3/T4/T5；P3-T5/T6 |
| A17 撤权与作用域 | P2-T7；P3-T4/T5/T6；P4-T1/T3/T5 |
| A18 降级 | P1-T5/T6/T8；P2-T3/T4；P3-T1/T4/T5；P4-T4 |
| A19 全层删除 | P2-T5/T6/T7；P3-T5；P4-T5 |
| A20 人工治理 | P2-T7；P3-T6；P4-T4/T5 |
| A21 重处理 | P3-T6；P4-T4/T5 |
| A22 安全渲染 | P2-T7；P4-T1/T3/T5 |
| A23 资源上限 | P1-T1/T5/T8；P2-T3/T4；P3-T4 |
| A24 离线运行 | P1-T7/T8；P3-T1；P4-T6 |
| A25 构建一致 | P1-T2/T7/T8；P3-T3；P4-T1/T6 |
| A26 Schema V1 | P2-T1；P4-T6 |
| A27 检索质量 | P4-T6 |
| A28 字节配额 | P2-T1/T2/T3/T4/T5/T6；P4-T5 |
| A29 来源与参数 | P1-T6；P3-T2/T6；P4-T2 |
| A30 文件身份 | P3-T3/T6；P4-T2/T5 |

合计 27 项实施任务：P1 8项、P2 7项、P3 6项、P4 6项。建议按 P1→P2→P3→P4 顺序实施；每个任务完成后检查规格符合性和代码质量，再进入下一任务。P1 的操作系统禁网能力先做可行性验证，失败时先解决执行环境，避免完成全部移植后才发现部署不可运行。

交付记录只填写实际命令、运行环境、通过/失败/跳过数和产物路径。生成计划不等于代码、数据库、浏览器、模型或部署已经验证。

## 6. 本次计划自审记录（2026-09-01）

- 已逐项对照规格，将 A01–A30 映射到 27 个实际任务号；跨计划任务引用和相对文档链接均可解析。
- 已用 Python AST 检查 61 个 Python 示例，用本项目 TypeScript 编译器检查 7 个 TypeScript 示例，未发现语法错误；接口清单使用text代码块，不伪装为可运行Python。
- 已扫描无未填写的占位项、尾随空白；校准真实后端浏览器路径、M11归档引用与数据库测试runner。
- 这些是计划文档静态检查，不代表示例已运行、类型已完整联调或业务测试已通过。没有实施业务代码、安装依赖、操作数据库、调用模型、提交或部署。
