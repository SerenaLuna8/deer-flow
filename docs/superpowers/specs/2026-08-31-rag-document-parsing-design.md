# RAG 文件解析重构规格：Dify 解析器复用与本地 Unstructured

> 状态：设计草案，待用户审阅；不代表实现已完成，不授权修改数据库、提交或部署。
> 日期：2026-08-31。
> ActWeave 核对基线：`b96581974b057c0ae4d853815130d99c0ed23823`。
> Dify 源码基线：`9c16c865977e9d89a9ec7ae0536e893f4385a758`，本地 `/Users/jiangfeng/dify`。
> 本文是目标行为和验收规格，不是逐步实施计划；批准后再使用 Superpowers writing-plans 拆解实施任务。

## 1. 目标、事实与范围决定

本次将现有知识库解析改造成“数据源类型 → ETL 模式 → 扩展名”的路由架构，固定版本复用 Dify 的具体解析类，通过本项目的适配器接入存储、权限和任务系统。主流格式保留表格、链接、图片和来源位置；长尾格式只使用可本地执行的 Unstructured 路径。所有解析器输出同一内部 `Document` 表示，再进行结构保护与 Token 切分。

### 1.1 已确认事实

- 当前项目的解析、清洗、切分入口是 `ingestion/preview.py::extract_clean_split`；预览和 Worker 摄取复用它。预览不写数据库、MinIO 或任务队列。
- 原文件属于 Knowledge Document；PostgreSQL 是身份、版本、授权、分段和任务的权威，MinIO 只保存字节。Worker 使用任务租约和文档版本检查后原子发布。
- 当前分段按字符计算且不跨 `ExtractedBlock` 合并，Markdown 按纯文本读，CSV/XLSX 不为每条数据附加表头。Word 已支持正文顺序和嵌套表格文字提取，这些能力不能倒退。
- Dify 的解析类不是独立可安装的 Dify 解析 SDK；PDF、Word、Excel 直接依赖其 storage、UploadFile 和数据库上下文，不能不加适配地导入本项目。
- Dify 当前 splitter 虽使用 `max_tokens` 命名，实际绑定 `len(text)`；本文要求的 Token 切分是新增行为，不能以“照搬 Dify 已有 Token 实现”验收。
- Dify PDF 默认读取文字层和嵌入图片，不做 OCR；图片持久化与多模态 Embedding 是两个不同阶段。
- M11 已有部分摘要和系统设置 schema/契约，但核对时宿主配置读取仍走 YAML。不能把 M11 设计文档等同已完成实现，也不能依据旧指南中的表数量推导当前 schema。

### 1.2 本草案采用的范围决定

| 决定 | 本规格的确定行为 | 确认状态 |
| --- | --- | --- |
| D01 解析联网 | 禁用全部解析 API，包括自建内网 Unstructured API；解析过程中不下载模型、字典、Pandoc、外链图片 | 已向用户询问，暂按更严格的本地范围起草 |
| D02 数据源 | 本期交付 FILE；路由先判断数据源，但不实现 Notion、网页抓取、邮箱账号同步 | 按“各种文件解析”主请求收敛；新数据源应另立规格 |
| D03 图片能力 | 交付图片提取、持久化、分段绑定、受控预览和展示；本期不交付 OCR、图片向量或视觉模型回答 | “为多模态检索/展示铺路”解释为完成附件基础能力，不宣称图像内容已可检索 |
| D04 Token 口径 | 新解析采用固定、本地、可复现的知识库 Tokenizer，明确不同于目标模型的计费 Token | 具体口径见第 6 节，随规格一并审阅 |
| D05 复用 | 固定 Dify commit 移植解析器源码，保留来源与最小修正记录；不在运行时依赖 Dify 服务或用户本机源码目录 | 选定方案 |

用户第 2 点中的“API 卸载到独立服务”与 D01 不同时实现。未来可以新增 ETL Adapter，但本期没有 API 配置项、客户端或隐藏降级调用。D01 若变更，必须同步修改网络、凭据、部署与验收契约，不仅修改一句描述。

## 2. 方案比较与选定架构

| 方案 | 结果 | 结论 |
| --- | --- | --- |
| 固定版本源码移植 + 宿主适配 | 复用具体格式处理逻辑，隔离 Dify 的数据库/存储/网络耦合，能够逐文件追踪差异 | 选定方案 |
| 运行时直接导入整个 Dify 后端 | 带入 Flask/SQLAlchemy 上下文、Dify 模型与存储配置，部署依赖大且所有权冲突 | 不采用 |
| 只模仿算法、全部重新实现 | 可独立维护，但不满足本次“直接使用内部解析器替换”的要求 | 不作为本规格的替代交付 |

外部 Interface 保持小：调用方提交已授权的本地文件与冻结解析配置，获得统一 Document 列表、附件描述和警告。格式差异、上游修正和本地依赖藏在解析模块内。

为便于维护和升级比对，移植记录保留上游仓库、完整 commit、原文件路径、原文件 SHA-256、移植日期、本地补丁说明及采用/排除的依赖。后续升级固定版本进行，不跟随上游 main 自动更新。

### 2.1 路由规则

1. 判断 `datasource_type`。本期仅接受 `file`，其他值返回明确的“不支持的数据源”，不尝试下载 URL。
2. 读取服务器有效 ETL 模式：`dify` 或 `unstructured_local`；默认 `dify`。这些是本项目受约束的枚举，不沿用可任意填写的 Dify 字符串。
3. 以规范化扩展名在显式注册表中查找 Adapter。扩展名之外还检查容器/文件签名是否与声明类型一致；错配失败，不把未知二进制兜底读成文本。
4. Adapter 输出统一 `list[Document]`；下游不得依赖扩展名选择自己的解析逻辑。
5. 注册表同时生成支持格式响应和服务端准入规则。一个 Adapter 的登记必须包含扩展名、适用 ETL 模式、依赖探测和固定样例测试。

本期不增加“自动/手动解析”第三个产品开关。`general/parent_child` 是后续切分模式，不参与选择 Markdown 解析器。

### 2.2 格式矩阵

| 扩展名 | `dify` | `unstructured_local` | 输出预分段 |
| --- | --- | --- | --- |
| `.txt` | Dify TextExtractor | 同左 | 全文，保留行位置映射 |
| `.md/.markdown/.mdx` | Dify MarkdownExtractor + 内容保护修正 | Dify UnstructuredMarkdownExtractor 的本地路径 | 标题节；MDX 不执行代码 |
| `.pdf` | Dify PdfExtractor | 同左 | 每页；图片为该页附件，不宣称恢复图文相对版面 |
| `.docx` | Dify WordExtractor | 同左 | 有序正文和表格，携带段落/表格来源映射，之后按章节组织 |
| `.xlsx/.xls` | Dify ExcelExtractor | 同左 | 每个工作表的数据行；XLS 不承诺图片提取 |
| `.csv` | Dify CSVExtractor | 同左 | 每个数据行，列名和值绑定 |
| `.html/.htm` | Dify HtmlExtractor + 安全规范化 | 同左 | 有序正文；不执行 JS、不加载外部资源 |
| `.pptx` | Dify UnstructuredPPTXExtractor 本地路径 | 同左 | 每页 |
| `.epub` | Dify UnstructuredEpubExtractor 本地路径 | 同左 | 章节/标题块 |
| `.eml/.msg/.xml` | 不支持 | 对应 Dify Unstructured 本地 Adapter | 标题/正文块；保留库实际提供的位置 |
| `.doc/.ppt/.odt`、未知扩展名 | 拒绝并提示转换格式 | 同左 | 无文本兜底 |

PPTX/EPUB 在 `dify` 模式保留是本项目兼容性决定：当前项目已支持，不能为了逐字复制 Dify 默认白名单而删掉。旧版 DOC/PPT 的 Dify Adapter 依赖 API，本期不移植；这不代表 Unstructured 库在其他集成方式下必然无法本地解析它们。

Unstructured 的粗分块仍可能使用字符参数，那只是第一阶段预分段预算；所有输出还必须经过本项目第二阶段 Token 校验和切分。

## 3. 目标代码组织与职责

以下是目标位置，不表示文件已经存在。已有 ingestion、tasks、storage 和 retrieval 不另建平行体系。

| 位置（相对仓库根目录） | 职责 |
| --- | --- |
| `backend/packages/knowledge/actweave_knowledge/extraction/contracts.py` | 内部 Document、ExtractSetting、SourceSpan、AttachmentDraft、ParseWarning 与 Interface |
| `.../extraction/base.py`、`processor.py`、`registry.py` | BaseExtractor、三级路由、格式能力清单 |
| `.../extraction/dify/` | 固定版本移植的 Text/Markdown/PDF/Word/Excel/CSV/HTML 解析器 |
| `.../extraction/unstructured_local/` | 只含本地分支的 PPTX/EPUB/Markdown/EML/MSG/XML Adapter |
| `.../extraction/normalizer.py`、`runtime.py` | Markdown 规范化、警告、隔离执行、资源限制 |
| `.../extraction/UPSTREAM.md`、`patches.md` | 来源、版本、文件摘要及上游差异 |
| `.../ingestion/splitter.py`、`tokenizer.py` | 结构感知的第二阶段切分、Token 计量；不是另一个文件解析器 |
| `.../ingestion/preview.py`、`pipeline.py` | 共用解析处理路径；分别使用预览附件接收器与持久化接收器 |
| `.../storage/`、`.../persistence/`、`.../tasks/` | 提取结果、附件、缓存和清理的所有权及生命周期 |
| `backend/app/knowledge/` | 宿主配置、授权、Gateway DTO 和 Worker 组合 |
| `backend/app/quotas/integration.py` 及宿主配额 Adapter | Knowledge 原文件/派生对象的字节预留、结算和校准汇总 |
| `frontend/src/core/knowledge/`、`frontend/src/components/projects/knowledge/` | 严格 DTO、动态格式列表、分段与附件展示 |

`actweave_knowledge` 不得导入 `app.*`、`deerflow.*` 或 Dify 的 `models/extensions/core.file`。移植代码对外部能力的调用由注入的 Adapter 完成；不通过 monkey patch 全局 Dify 模块伪装兼容。

### 3.1 统一 Document 契约

内部 `Document` 与数据库里的 Knowledge Document 不是同一概念：前者是一份原文件的一个预分段，后者是用户上传的源文件。仅在 extraction 内使用 `Document` 名称，对其他模块可导出别名 `ExtractedDocument`。

| 字段 | 约束 |
| --- | --- |
| `page_content: str` | 规范化 Markdown；UTF-8，可复现，不含对象存储地址、签名 URL、临时路径 |
| `metadata` | 严格内部结构：有序 `source_spans`、`heading_path`、结构类型；不接受任意用户身份/权限字段 |
| `attachments` | 有序 AttachmentDraft 引用；同一图片可有多个出现位置 |
| `warnings` | 稳定错误码、位置和不含敏感细节的说明；不能用一条日志代替用户可见警告 |

`SourceSpan` 至少能表达已有的 page、paragraph、table/row、sheet/row、slide、chapter；统一对外从 1 开始。无法获得的位置留空，不编造页码。合并或拆分后的段保存覆盖的来源列表，兼容投影中的 `source_position` 取第一个位置，新增 `source_spans` 承载完整信息。

每个最小结构块必须有稳定 block_id、规范化 Document.page_content 中的起止字符偏移，以及对应 SourceSpan。清洗、插入标题/重复表头、第二阶段切分均同步维护偏移映射；插入内容标为 context_prefix 并指向原标题/表头位置。最终段只携带实际覆盖的来源，不能给每段附上整章全部段落号。这里的偏移用于追踪，不要求恢复 PDF 页面二维坐标。

BaseExtractor 的逻辑 Interface 是 `extract(setting, context) -> list[Document]`。Context 只提供：附件接收器、取消/预算检查、本地运行时资源；解析器不拥有事务、项目授权、模型调用或任务状态变更。

附件接收器接收提取出的本地字节流与出现位置，返回稳定逻辑引用。预览接收器仅使用请求临时目录；正式摄取接收器通过宿主编排登记并持久化。提取器可在解析过程中逐张交付图片，避免先在内存收集所有图片。

子进程里的接收器是 IPC Adapter：图片写入本次工作目录，发送受限相对路径、摘要、尺寸与出现位置给父进程，父进程验证路径归属/禁止符号链接和预算后才登记并 PUT。子进程不接收数据库、MinIO 或模型凭据，不通过“注入接收器”间接在子进程里连接这些服务。父进程停止接受新附件后，先终止/回收解析子进程，再等待已开始的 MinIO 调用结算，最后才能清理 staging。

## 4. Markdown 与结构保持规则

“万物皆 Markdown”在本规格中指统一的**内容表达格式**，不是把所有语义压进一个字符串。来源位置、附件身份、版本和警告仍是结构化字段；检索向量不包含图片字节。

### 4.1 各格式的必要行为

- Excel/CSV：每行生成列名和值明确绑定的 Markdown 字段列表；包含工作表/数据行定位。空值保留为空，空表头使用稳定列名（如“列 B”），重复表头按列位置消歧。表头启发式可以复用，但必须给出 `HEADER_INFERRED` 警告；没有可信表头时按列名生成，不能吞掉第一条数据。解析错误行不得静默跳过，CSV 的 `on_bad_lines="skip"` 必须移除；CSV 值按字符串保留，`00123`、`NA`、空单元格不能被 pandas 默认推断改写。
- Word：正文与表格顺序不变；Heading 样式转标题；提取嵌套表格；不因格式 Run 边界删除空格，不去重正文中本来重复的句子。普通段落可以在章节内组合，段落来源仍可定位。
- Markdown：保留标题级别和祖先标题路径；代码围栏、行内代码、泛型、尖括号占位符不被 HTML 清理正则删除。MDX 仅作为文档源，不执行组件或表达式。
- 表格：短表保留 Markdown 表格；长表按行拆分时重复表头和表题。无表头的表格不强制把首行当标题，转为带列位置的字段列表。单行过长时按单元格继续分，保留字段名和原行定位。
- PDF：保留逐页输出和页码；解析图片置于页内可确定的关联位置，只有页信息时明确标注“本页图片”。不声称准确恢复图片与段落的二维关系。
- HTML/XML：禁止执行脚本与外部实体/网络加载；保留可用标题、列表、表格与链接文字，移除活动内容。文本与代码中的字面 `<...>` 不被当作 HTML 一概删除。
- Unstructured 输出：使用实际元素类型/metadata 转换；表格 HTML 可以安全转 Markdown；缺失结构时保留文字并给出警告，不能凭空生成表头、章节或页码。PPTX 无 page_number 的元素不能直接丢弃；邮件不得对 partition 已解码的正文再猜测 Base64 解码。

移植器的允许补丁仅包括宿主解耦、本地执行限制、安全/内容完整性修正和来源增强；每类补丁必须有对应样例，不能用一次大重写失去可追踪性。

### 4.2 文本编码与表头选择

TXT、Markdown、CSV 统一使用：BOM 识别（UTF-8/UTF-16）→ 严格 UTF-8 → 固定版本 charset-normalizer 探测并严格解码。记录最终 encoding；使用探测结果时返回 `ENCODING_DETECTED`。空候选、探测超时或严格解码失败都明确报解析失败，不使用 errors=ignore/replace 静默丢字。探测只读取最多 1 MiB 样本，在解析子进程内最多 5 秒；选定编码必须对完整文件严格解码。HTML/EPUB 按格式内编码声明解析，不能套用猜编码覆盖有效声明。

表格解析增加冻结参数 `header_mode=auto|none|explicit`（默认 auto）与可选表头行映射；CSV 对应一个行号，Excel 按工作表分别配置。auto 沿用“前 10 行首个至少两个非空文本单元格的候选”规则，无候选则 none，不使用非空数量最多的业务行兜底。自动候选及原始表头行必须在预览中完整显示并给 warning，用户可切 none 或显式行号；自动检测不能被宣传为准确识别。没有列名的有数据列补稳定列名，表头之前的非空说明行作为上下文保存；原始表头行及其位置进入来源结构，不能不可追踪地丢弃。参数改变使预览和提取缓存失效。

### 4.3 展示内容与索引内容

Knowledge Segment 的 `content` 保存规范化 Markdown，引用正文和编辑器均使用它。新增派生 `index_text`：保留标题、字段名、值、代码和链接可见文字，移除附件逻辑 ID、URL 技术参数与 Markdown 控制符。该转换固定版本并在内容写事务中派生。

Embedding、词法索引和文本 Reranker 使用同一版本 `index_text`。Agent 得到 Markdown 正文和受控附件描述；图片 ID/哈希不参与语义召回。不能通过改变 index_text 改写用户看到的事实。

纯图片文件页若没有实际文字且本期无 OCR，应报告 `NO_INDEXABLE_TEXT`，不能仅因为存在图片链接就标成已可检索。图文混合文档可 ready，并显示“图片已保存，图片内容尚未进入文本检索”。

## 5. 图片所有权、身份与持久化

### 5.1 领域实体与 schema

新增三个 Knowledge 域实体；在实现时同步补充 CONTEXT.md，避免使用 Dify UploadFile 作为本项目宿主表的名称。

| 表/字段 | 最小内容与约束 |
| --- | --- |
| `knowledge_extractions` | id、project/base/document、source_sha256、parser_fingerprint、normalization_version、state（staging/ready/deleting）、manifest_storage_key、manifest_sha256、manifest_size_bytes、manifest_upload_state（pending/stored/delete_pending）、创建时间、创建 task/attempt、unpublished_expires_at、delete_error |
| `knowledge_attachments` | id、extraction_id、project/base/document、sha256、media_type、size_bytes、width/height、storage_key、state（staging/ready/deleting）、delete_error；同一 extraction 内内容哈希唯一 |
| `knowledge_segment_attachments` | project/base/document、segment_id、attachment_id、position、alt_text；复合外键保证附件与分段属于同一源文档 |
| Knowledge Document 新字段 | source_sha256、published_extraction_id、冻结 parsing_profile；必要的解析警告与能力版本投影 |
| Segment/Child 新字段 | index_text、token_count、source_spans；既有 content_digest 继续绑定展示正文 |

所有对象 locator 仅存数据库，浏览器、模型输入和日志均不得出现。附件只能通过项目身份和已发布分段关系授权，不接受客户端给出的 project_id/对象 key 作为权限依据。

不做跨项目、跨文档去重。提取结果拥有附件；文档内重复图片共用字节，出现位置独立保留。Excel 的工作表/锚点属于出现位置，不能通过去重删掉第二个位置的图片。

对象键由服务器生成，例如 `projects/{project}/knowledge/{base}/{document}/extractions/{extraction}/assets/{sha256}.{ext}`；新的 key grammar 与旧原文件 grammar 分开验证。SHA-256 用于内容一致性，不用作浏览器访问凭证。

Markdown 使用逻辑形式 `![说明](knowledge-attachment:<ref>)`。ref 为安全规范化后图片字节的 SHA-256，在所属 extraction 中解析为 attachment 行；它不含 attempt/extraction UUID，因此同源文件、同策略的预览与摄取可以产生相同正文。规范化版本变化影响缓存 fingerprint，最终图片字节变化影响 ref 与正文 digest。出现位置单独记录，不因相同哈希去重丢失。服务端验证 ref 与分段绑定后，前端才转换为受控图片资源；猜中摘要不授予访问权。

### 5.2 正式摄取状态转换

1. Worker 在有效 claim 下读取原文件摘要与解析快照，寻找同文档、同 fingerprint 的 ready 提取结果；命中则复用，无需再次运行格式解析器。
2. 未命中时，为当前 task/attempt 登记 staging extraction。附件 PUT 前先登记其服务器 key 和 staging 行；执行网络 I/O 时不持有数据库事务。
3. 提取完成后，将完整 Document 列表、来源映射、附件清单和警告保存成 manifest；校验对象完整性后标记 extraction ready。这里的 ready 仅表示提取结果完整，不表示文档已发布可检索。
4. 清洗、结构保护、Token 切分并批量生成向量；每次实际 Provider 请求及重试前复验项目状态和租约。
5. 在一个带版本/租约检查的发布事务中替换 Segment/Child、附件绑定、词法派生，更新 published_extraction_id、published_version 和 Document ready，并结算 task。
6. 发布失败或失去租约不能暴露 staging/未绑定附件。完整 ready extraction 可以作为该文档重试的缓存；不完整结果必须进入耐久清理。

任何 cleanup 都不得删除其他 attempt 的 staging 结果、当前 published extraction 或被正在执行的索引任务引用的提取结果。每文档保留当前 published extraction，以及至多一个不同的、最新完整未发布 extraction 供失败重试；后者自完成时起保留 24 小时，以数据库时间判定到期。更旧或到期且未引用的结果进入清理任务；正在运行的索引任务会推迟回收。手工分段只允许引用当前 published extraction 的附件，避免产生额外提取世代引用。

新建 `delete_extraction` 类型的 Knowledge Task，resource_id 只能是经作用域校验的 extraction ID，由服务器枚举已登记的附件/manifest key。不得扩大现有 `delete_document_object` 的参数范围使之接受任意派生路径。Worker 启动恢复与定期维护将已失去 claim 的 staging 和到期未引用结果排入同一耐久清理；没有静默的进程内 fire-and-forget GC。

存储删除顺序是“删对象 → 确认 → 删关系/行”，保留失败记录供重试；不靠数据库 CASCADE 当作字节已删除的证明。文档/库删除和 Project 最终删除都覆盖原文件、manifest、附件及未完成提取结果。Knowledge 禁用后，既有 Project retention 清理能力继续存在。

### 5.3 失败降级与资源限制

可降级为 warning：单张内嵌图损坏、不支持的编码、图片像素或数量超限、外链图片未抓取。必须保留位置和明确占位说明，不能伪造图片内容。

不可当作图片 warning 忽略：MinIO 不可达、权限不足、数据库写失败、租约过期、授权撤销、文档版本冲突。这些走既有失败/重试/冲突流程，避免“文本成功”掩盖失控对象。

本期固定保护上限（不是整进程内存保证）：

- 原文件继续 ≤50 MiB，MinIO 每次单 PUT，复用现有每 store 单上传槽。
- 提取正文最多 5,000,000 字符；父段及实际向量条目分别不超过当前每文档 5,000 配额。
- 图片最多 100 个独立字节对象，每张 ≤5 MiB、≤20,000,000 像素，单文档图片合计 ≤50 MiB。重复出现不重复计字节对象，但出现次数纳入 manifest 大小限制。
- manifest 规范 JSON ≤50 MiB，不截断后伪装完整缓存；超限明确失败。
- 每次解析工作目录合计 ≤512 MiB（原文件、提取图片、manifest 与转换临时文件），超限终止本次解析后按状态清理；并发总临时空间按已配置任务槽数预留。
- 浏览器展示统一使用安全栅格输出（PNG/JPEG/WebP）；其他图像格式在本地受限转换，不执行 SVG、脚本或动画；动画只取首帧时给 warning。
- 原文件、图片字节和 manifest 一并纳入项目存储用量预留、结算及删除释放，不能形成不受配额约束的旁路。

### 5.4 项目字节配额接入

当前宿主的存储校准主要统计 PrivateFile 和 Project Skill 文件，Knowledge 上传目前只检查文档数量；本期必须补齐，不假定已有附件配额支持。包内通过宿主注入的 `KnowledgeStorageQuotaPort` 调用 `reserve(project_id, object_id, size_bytes)`、`commit(object_id)`、`release(object_id)`；身份来自服务器，接口不得接受用户自选对象 key。

每个原文件/附件/manifest 使用固定数据库对象 ID 作为唯一预留键；已知最终字节数后、PUT 前在短事务内预留，重复 reserve/commit/release 幂等。字节数或目标不一致的相同 ID 视为冲突。预留不足时不得 PUT；已开始 PUT 的失败须先确认对象不存在或完成清理，才能释放。

宿主配额校准同时汇总 KnowledgeDocument.size_bytes、已登记 Attachment.size_bytes、Extraction.manifest_size_bytes。未确认持久化的对象计为预留，已确认持久化且未确认删除的对象计为已用；以各对象上传事实和宿主账本判定，不能仅凭父级 Document/Extraction 还在 staging 就把已写入字节算成零。manifest_upload_state 独立于 Extraction 是否完整；同一对象只进入一种用量状态，不能两次计量。已删除但未释放的结算记录由校准修复；对象未确认删除时不能因任务失败被减掉。读取校准使用同一对象登记事实与宿主账本，不通过猜测 MinIO listing 反推权限。

新增/扩展 Knowledge 上传与 `app/quotas/integration.py` 的校准测试，证明一次校准不会抹掉 Knowledge 用量，缓存命中不会重复增加用量，删除失败仍保留用量。此项与附件 schema 同包交付。

## 6. 两阶段切分与 Token 契约

### 6.1 Token 定义

为避免把所有模型的 tokenizer 接口强行引入解析器，本期使用固定的本地 `cl100k_base` 作为**知识库切分 Token**；初始依赖候选为 Dify lock 中已有的 `tiktoken==0.12.0`，实施时在 Python 3.12 与目标镜像中验证并锁定。编码数据在构建/安装时固化到只读资源包并登记 SHA-256，运行时不得触发下载。

Tokenizer Profile ID 固定为 `knowledge-cl100k-v1`，代码/词表摘要进入 parsing_profile。它是产品的可复现分段单位，**不声称等于 Qwen、其他 Embedding/Reranker 或 LLM 的真实输入 Token，也不用于费用估算**。Provider 输入超限仍明确失败，不静默截断；未来若要求模型精确 tokenizer，需要单独扩展模型注册契约。

新上传默认：父段 1000 Token、重叠最多 100 Token；父子模式子块 500 Token、子块零重叠。父段范围 200..4000、重叠 0..500 且小于父段、子块 100..2000 且小于父段，单位均为知识库 Token。同时保留父段 4000 字符硬上限，避免突破现有人工编辑与工具返回预算；因此这些数值是上限，实际段可以更小。

DTO 必须携带 `chunk_size_unit` 与 `tokenizer_profile_id`，UI 明示“知识库 Token”，字符统计 `word_count` 不改成 Token。历史字符参数不能被重解释为 Token；逻辑兼容见第 10 节。

### 6.2 切分算法要求

1. 第一阶段：解析器按 PDF 页、表格行、标题节、Word 有序内容、幻灯片或 Unstructured 元素群形成 Document。
2. 规范化阶段：识别标题、段落、字段列表、表格行、代码块、图片引用等可追踪结构；不需要构建包含任意文档格式属性的通用 AST。
3. 第二阶段：优先按结构边界打包，在 Token 和字符双预算内形成 Segment。每个最终段补充必要标题路径/表头，补充内容也计入预算。
4. PDF 默认不跨页；Word 可在同一标题节组合相邻段落。Excel/CSV 不将不同数据行混为一段；Markdown 子节不丢父标题。不是无条件合并全部 Document。
5. 超长代码块按行切分并补全围栏/语言标记；单行仍超长时按完整 Unicode 边界切分。表格超长行按字段切，保留列名、原行号，不能切断 UTF-8、图片 ref 或链接语法。
6. 图片 ref 是不可拆原子，附加到原结构块；若原块继续切分，绑定到包含该出现位置的段。不以“每段都附上本页全部图片”伪造精确关联。
7. overlap 按完整结构片段保留，最多指定 Token 数；不跨提取页/数据行边界重复，不生成纯重叠段。
8. parent_child 的父段与 general 共用结构规则；子块只在父段内切，返回/引用父段。父子模式不是本期的全文父块模式，不扩大既有 Agent 64 KiB JSON 预算。

现有 `chunk_separator`、`child_chunk_separator` 保留并分别冻结：结构边界优先，用户分隔符仅在可拆的普通文本内部优先于固定 fallback 生效，不打碎表格行、代码围栏、链接或图片原子。沿用仅解码 `\\n/\\t/\\r` 的规则，不能对中文分隔符使用会损坏字符的 unicode_escape。父子两级有各自分隔符；不能保留 UI 配置而实现中忽略它。

Token 数使用待嵌入 index_text 计算，同时校验 Markdown 展示正文 Token 数；两者都不得超过对应上限。移除 URL/邮箱规则只作用于普通文本和显式外部链接，不吞掉内部图片 ref，不改写代码块。规范化、清洗、切分版本全部进入快照。

## 7. 预览、附件读取与 PDF 缓存

### 7.1 无持久化副作用预览

上传预览继续同步返回，使用请求临时目录与 PreviewAttachmentSink，不创建 Knowledge Document、extraction、attachment、query 或 task 行，不写 MinIO。请求结束必须清理临时文件；已启动的解析工作退出后才删除其目录。

预览响应返回前 10 个父段、完整总段数、Token/字符数、子块、source_spans、warnings、解析/切分 fingerprint。仅返回这 10 段中最多 20 个图片缩略图，每张 ≤128 KiB、合计 ≤2 MiB；缩略图是响应内字节的编码表示，前端转 Blob URL，不写 localStorage 或持久查询缓存。超出仅省略缩略图并返回数量，不更改正式分段或附件绑定。

这项“仅预览缩略图降采样”是预览与正式摄取唯一允许的图像表示差异；Markdown、位置、警告、逻辑图片身份与分段结果必须相同。临时 Blob URL 不进入 content_digest。

分段预览身份新增有效 ETL、解析器 fingerprint、Tokenizer profile 和规范化/切分版本。预览 fingerprint 是服务器计算的规范化摘要：原文件 SHA-256、规范化扩展名、完整解析/表头/图片/清洗/切分参数、能力和资源版本均参与；它不包含临时路径或 Blob URL。上传重新计算原文件摘要再比较，不能拿文件 A 的预览用于同参数的文件 B；fingerprint 不是权限凭证。用户换文件、参数或有效能力版本后，旧预览标为过期；网页上传携带预览 fingerprint，服务器不匹配则返回冲突并要求重新预览。API 上传不强制先预览：未传预览 fingerprint 时，服务器冻结当前有效配置并在响应中返回；一旦传入则必须严格校验，不能忽略不匹配。

从原文件重解析的预览可以读取现有完整缓存，但不能在缓存未命中时写入新缓存。

### 7.2 发布后附件读取

新增受项目授权保护的管理附件读取端点：`GET /api/projects/{project_id}/knowledge/documents/{document_id}/segments/{segment_id}/attachments/{attachment_id}`。引用用途使用 `GET /api/projects/{project_id}/knowledge/bases/{base_id}/documents/{document_id}/segments/{segment_id}/attachments/{attachment_id}`，与当前带 Base 的引用详情路径一致。两者都绑定 expected_document_version、expected_content_digest，并复用对应 Segment 详情的授权与版本校验，确认该 Segment 实际引用此图且属于 published_extraction_id。无权限/删除/版本变化按既有 403/404/409 规则处理。

检索引用上下文要求文档 ready 且文档/分段启用。管理浏览沿用现有维护可见性：可以查看停用内容，以及失败 reparse/reembed 留下的已发布内容和图片；这不是恢复其检索资格。此时附件绑定的是 Segment 的已发布世代，不能把 Document 最新的失败 target version 当成图片所属版本。具体管理/引用用途来自对应服务入口，不能用一个用户可控标志跳过授权。没有 Segment 绑定的缓存附件不通过该端点公开。

不下发 MinIO 签名 URL。Gateway 完成对象复制后再次校验授权与版本，响应 `Cache-Control: private, no-store`、明确 MIME、`X-Content-Type-Options: nosniff`；前端经认证 fetch 转 Blob URL，切换项目/关闭详情时释放。

原 Markdown 中的外部图片不自动加载；安全链接可以由用户主动点击打开。Markdown 禁止 raw HTML、脚本、事件属性及 `javascript:` 等协议。引用面板显示图片不表示当前 Agent 已看到图片字节。

### 7.3 PDF 缓存

PDF 缓存复用完整 ready extraction，不另建一份不受治理的明文缓存。缓存内容是序列化 `list[Document]`、source_spans、附件清单、警告和格式版本，而不是把所有页拼成字符串。

缓存键包含 document_id、原文件 SHA-256、实际 extractor ID/版本、规范化版本、表头策略、图片策略及上游依赖 fingerprint；**不包含 chunk_size、overlap、清洗开关、Tokenizer**，因为缓存位于这些处理之前。改变切分参数可以重用解析结果，改变解析器、表头或图片策略必须失效。

只有引用的附件和 manifest 都完整时算命中；缺失/损坏在有效 Worker claim 下重新提取，不返回部分缓存；权限失败不能当作缓存未命中绕过。命中后仍应用本次冻结的所有预算，旧缓存不能绕过收紧后的配额。缓存关闭时新任务不读取旧缓存，但既有发布附件仍可展示，旧结果按引用和保留策略清理。该提取结果复用机制适用于同文档的其他格式，PDF 是必验场景；不再为每个格式建独立缓存模块。

## 8. 配置、接口与前端

配置沿用 M11 的 PostgreSQL Knowledge 系统设置方向，只有一份权威来源。本规格实施接入前须先完成/核对 M11 配置读取链路；不得在 YAML、环境变量和管理页各新建一份 ETL 配置。配置保存仍按宿主约定重启生效，不承诺热切换。

系统设置新增最小字段：`etl_type`（dify/unstructured_local，默认 dify）、`extraction_cache_enabled`（默认 true，覆盖 PDF 等提取结果）。保护性上限和 Tokenizer 初始版本是包内固定契约，不增加任意路径/远程地址输入框。

文档上传/重解析准入固化 parsing_profile：effective_etl、extractor_id/version、source_kind、normalization/cleaner/splitter versions、chunk_size_unit、tokenizer_profile_id/digest、父/子分隔符、表头参数及图片策略。重试沿用原快照；软件不再支持原 fingerprint 时明确要求用户重解析，不悄悄选新解析器。

新增 `GET /api/projects/{project_id}/knowledge/file-capabilities`，遵循既有项目读取权限。返回有效 ETL、capability_revision、每种扩展名的 parser_id、available/reason_code、是否支持内嵌图片、切分参数单位和上限；不返回磁盘路径、依赖错误堆栈、存储配置或密钥。

- 前端 accept、格式提示、上传校验和预览使用此响应，不再硬编码扩展名。
- 某格式依赖缺失时明确不可用，不静默换解析器或吞掉格式；必需主流解析依赖缺失则拒绝启用解析功能。Gateway 与 Worker 构建 fingerprint 必须一致。
- 分段预览、详情、检索命中正文使用同一安全 Markdown 渲染模块；编辑使用 Markdown 源文，附件以受控选择器插入，不允许自由引用其他文档附件 ID。
- CSV/Excel 预览显示自动表头候选、工作表与实际行号，提供 auto/none/显式表头行选择；选择变化使用同一预览身份失效机制。
- 编辑/新增父段同步更新 index_text、Token 数、子块、附件绑定和词法派生；保留既有模型调用前授权检查和最终文档版本检查。
- 文件处理结果显示 warnings 及计数。用户能区分“已保存图片”“图片未提取”“文字已索引”“纯图片尚不可检索”。
- 项目只读成员可预览已发布内容但没有上传/编辑入口；上传型预览沿用写权限。晚到响应不得跨项目或覆盖新一轮预览。

## 9. 本地运行、依赖与隔离

主流解析依赖复用 Dify 对应库：pypdfium2、python-docx、openpyxl、pandas/xlrd、BeautifulSoup、charset-normalizer、图像库；长尾通过明确 extras 引入 Unstructured。实施时只移植所选解析器，不安装 Dify 全部后端依赖。

从上游 lock 提取候选版本，分别验证 Python 3.12、macOS 开发环境及生产 Linux 镜像后，写入本项目依赖和 lock。上游可运行不等于本项目已兼容；不允许仅写宽泛 `>=` 后宣称复用完成。

已核实的移植候选版本：pypdfium2 5.6.0、python-docx 1.2.0、openpyxl 3.1.5、pandas 3.0.2、xlrd 2.0.2、beautifulsoup4 4.14.3、charset-normalizer 3.4.7、unstructured 0.21.5、python-pptx 1.0.2、python-oxmsg 0.0.2、pypandoc/pypandoc-binary 1.17、python-magic 0.4.27。选定本地 extras 后锁定完整传递依赖，不引入整个 graphon 以间接获得解析库。

Unstructured 的依赖元数据可能包含 HTTP 客户端；“不用 API”的验收是生产路径不调用 API、不联网，不是依赖树里绝不能出现 requests/unstructured-client。可禁用/剥离的遥测必须关闭；精确资源和禁用方式以锁定 wheel 的源码及禁网运行证据确认，不能凭旧版本经验臆造环境变量。

Pandoc、libmagic、图像编解码器、Unstructured 使用的 NLP 资源以及 Tokenizer 数据，均在安装/镜像构建时准备并校验。删除移植代码中的 `download_pandoc()` 和其他运行时下载入口。确切运行时资源清单由隔离验收生成并入库；缺少资源时能力探测显示不可用，不能运行到一半才访问公网。

解析在受控子进程中运行，以终止损坏文件导致的阻塞解析；保留 worker_concurrency=2 的默认任务并发，Gateway 预览每进程最多同时运行 1 个解析子进程。队满返回明确可重试错误，不无界启动进程。原任务 900 秒默认超时仍包含下载、解析和模型阶段；预览解析上限 120 秒，超时终止并回收本次解析子进程后清理目录。

子进程处理不可信文件，禁用解析库的联网入口；离线验收还需在禁止外部网络的运行环境中证明。不能把 monkey patch 网络库当作生产安全隔离。需要的 MinIO I/O 和 Embedding/Reranker 调用由父级编排执行，不在解析子进程里进行。

本期不引入 LibreOffice 转换旧 Office 格式、不启动解析 API 服务、不执行用户文档的宏或脚本。ZIP 解压体积预检查、累计正文预算、图片解压像素限制都在加载/累积阶段执行；不能宣称这些等于严格的全进程峰值内存上限。

## 10. 重解析、重嵌入、摘要与部署

- 新文件使用新 profile；retry 保持快照。已经发布的段不因切换 ETL 或部署新版解析代码被自动改写。
- 文档 reparse 显式读取原文件/可用缓存，用新的确认参数替换全部 Segment/Child/附件绑定，并重建其派生索引；继续提示人工编辑和分段停用状态将被覆盖。
- base rebuild 仍只重新嵌入已发布内容，保留 Markdown、来源、附件映射、人工编辑和启用状态，不读原文件、不调用解析器、不生成新附件。
- M11 摘要是派生索引：文本变化/reparse 使旧摘要失效并按库开关重新排队；附件 URL、存储 key、图片字节不进入文本摘要输入。reembed 的摘要生命周期继续遵守 M11 契约。
- 逻辑兼容必须区分旧 `character` profile 与新 `token` profile；旧值不可原位改含义。旧发布内容可读；用户选择 reparse 时显示新单位和变化范围。兼容投影不能伪造旧文档的解析器版本。

**数据库发布限制：** 本仓库 Schema V1 没有增量迁移入口。新增实体/字段必须同时更新 ORM、Schema V1 SQL、catalog digest、中文注释、必需表检查与 schema 测试；只在新的空目标库验证安装。本文不包含对现有数据库执行 ALTER、自动修复或 reset 的授权。

若目标部署必须无损保留现有数据库，需要另行批准数据迁移方案后才能上线；上面的逻辑兼容要求是迁移方案必须满足的约束，不代表无损 DDL 迁移已经存在。新 schema 不能通过回退旧代码直接恢复，回退需配套原数据库备份/目标环境方案，不能自动降级 schema。

实施基线应在 M11 schema、系统设置和摘要生命周期整合完成后固定。并行实施只能在不冲突的 extraction 内部文件推进，合并前重新审查配置/发布/reembed 的实际代码；不得覆盖当前工作区其他任务改动。

## 11. 错误与观测契约

沿用 KnowledgeError 与现有 HTTP 映射，新增 reason_code 区分具体原因：`UNSUPPORTED_FORMAT`、`FORMAT_SIGNATURE_MISMATCH`、`PARSER_DEPENDENCY_UNAVAILABLE`、`PARSER_TIMEOUT`、`NO_INDEXABLE_TEXT`、`TOKENIZER_UNAVAILABLE`、`PARSER_PROFILE_UNAVAILABLE`。普通资源超限走 KNOWLEDGE_QUOTA_EXCEEDED，授权/版本/租约错误继续走原有分支。

图片降级使用结构化 warning，例如 `IMAGE_CORRUPT`、`IMAGE_LIMIT_EXCEEDED`、`EXTERNAL_IMAGE_NOT_FETCHED`；日志仅记录服务器资源 ID、parser_id、阶段、耗时、计数和安全错误码，不记录正文、URL 参数、对象 locator 或 Provider 原始报错。

任务进度由真实阶段更新：reading_source、extracting、normalizing_splitting、embedding、publishing；缓存命中单独记录，不伪造已解析页数。API 中已有阶段枚举的调整必须与前端和 replay 测试同批交付。

## 12. 验收规格

验收使用固定生成的小样本及有授权的真实文档。相同源文件/快照的冷解析、热缓存、预览和正式发布，必须比较规范化内容、来源和附件身份，而不是只比较段数量。

| 编号 | 场景与通过标准 |
| --- | --- |
| A01 路由 | 两模式每种允许格式命中唯一 Adapter；未知扩展名、伪装 ZIP/Office、DOC/PPT/ODT 明确拒绝；没有 API fallback |
| A02 隔离 | 新解析包不存在 Dify 宿主 import；原始来源文件、固定版本与补丁清单可追踪 |
| A03 Excel | 标题行+空行+表头、空表头有数据、重复表头、空值、公式缓存、多 sheet、图片锚点全部保留正确字段和实际行位置 |
| A04 CSV | UTF-8/UTF-16/GB18030 与探测编码；带逗号/引号/换行字段；错误行可见失败，不静默少行 |
| A05 Word | 标题—说明—表格—段落順序；嵌套/合并单元格；重复正文不去重；跨格式 Run 的空格保留；链接与图片不丢 |
| A06 Markdown | 父标题继承；C# 标题、List<int>、Map<K,V>、<IP>、代码围栏原文保留；长代码块闭合且可定位 |
| A07 PDF | 多页来源完整、嵌图出现位置可追踪；扫描页不伪造 OCR 文字；纯图片明确不能完成文本索引 |
| A08 长尾 | PPTX/EPUB 保持既有支持；本地 EML/MSG/XML 可运行；XML 外部实体和远程资源不访问 |
| A09 表格切分 | 每个长表分段仍有表头/列名和原行定位；长单元格拆开也能识别所属字段；无跨行内容混淆 |
| A10 Token | 中英混合/Emoji/代码按指定 Tokenizer 计量；所有父子段同时满足 Token 与字符硬上限；子块不重叠 |
| A11 内容派生 | Markdown、index_text、词法向量输入、Reranker 输入一致受版本约束；附件 ID 不进入检索文本；图片 ref 不被清洗或切断 |
| A12 预览一致 | 预览与摄取的正文/来源/警告相同；预览后数据库、MinIO、任务数零增长，临时目录清空；图片缩略图省略有计数 |
| A13 前端竞态 | 文件 A→B、同名替换、参数变更、ETL revision 变化、项目切换与晚到响应不污染当前预览 |
| A14 缓存 | PDF 冷热路径逐页、结构、附件、警告一致；改 chunk 参数命中；改 parser/source/image policy 失效；损坏缓存不能半成功 |
| A15 幂等 | task 重试/崩溃恢复不重复发布分段，不产生无主图片；相同图片多处出现保留多处引用但不重复字节对象 |
| A16 发布原子性 | 每个关键阶段注入异常，旧发布内容与新提取结果不会混用；失租约/版本竞争结果不发布 |
| A17 撤权 | 图片读取、解析开始前、Provider 前、发布前及下载后撤权均遵守既有授权规则；跨项目/跨文档 ref 被拒绝 |
| A18 降级 | 单图损坏可带警告发布文字；数据库/MinIO失败不可伪装为单图 warning；纯图片与零文本路径明确失败 |
| A19 清理 | 文档/库/Project 删除覆盖原件+manifest+附件+staging；删除失败可见且可重试；对象先于权威行删除 |
| A20 手工治理 | 修改/新增段会同步更新 Token、index_text、附件绑定、子块和派生索引；停用后不可通过引用入口读取图片，授权管理浏览仍可查看；失败重处理可查看旧发布图片且不会拼错版本 |
| A21 重处理 | rebuild 零原文件读取/零解析；reparse 原子替换且不保留错误旧附件绑定；M11 摘要按内容版本失效 |
| A22 安全渲染 | raw HTML、脚本协议、外部追踪图片不执行/不加载；受保护附件不暴露存储key/签名URL；Blob在离开作用域后释放 |
| A23 资源 | 字符、图片、manifest、并发、超时上限均有可复现样例；超时子进程已结束再清理目录，无后台继续写入 |
| A24 离线运行 | 在只允许业务 MinIO/模型连接且禁止解析子进程联网的环境中跑完整格式矩阵；没有运行时 pip、Pandoc、NLP/Tokenizer下载 |
| A25 构建一致 | Python 3.12/macOS 开发环境与生产 Linux 镜像的依赖及资源探测通过；Gateway/Worker capability fingerprint 相同 |
| A26 schema | 新空数据库 ORM/SQL/catalog/中文注释一致；未运行目标库reset/ALTER；既有schema漂移按原契约失败 |
| A27 检索质量 | 固定查询集包含字段定位、标题上下文、长表、跨Word段落和代码字面值；前后使用同Embedding/Reranker/配置，记录Hit@5与MRR@5；关键样例命中正确来源且不得回退，不能只展示主观截图 |
| A28 字节配额 | 原件、staging图片、ready/deleting对象、manifest各有幂等账目；校准后用量不消失、不重复；预留不足不PUT、删除失败不释放 |
| A29 位置与参数 | Word同章节三段合并后在第二段中间切开，两个结果只列真实覆盖来源；父/子自定义中文和转义分隔符生效，表格/代码/图片结构不被破坏 |
| A30 文件身份 | 使用文件A的预览fingerprint上传同名/同扩展名的文件B被拒绝；无预览的API上传可冻结服务器配置正常执行 |

计划新增测试文件：`backend/tests/knowledge/test_extractor_registry.py`、`test_dify_extractors.py`、`test_local_unstructured.py`、`test_markdown_chunking.py`、`test_knowledge_attachments.py`、`test_extraction_cache.py`；扩展既有 ingestion/upload/schema/worker/storage/governance/reembedding/retrieval 测试。前端新增能力与预览身份单测，扩展 `project-knowledge.spec.ts` 和真实后端知识库浏览器测试。

实施完成后需实际运行并记录的门禁：

- `cd backend && make format`、`make lint`、`make test`，以及知识库定向测试；纯样例、真实 PostgreSQL/MinIO 和外部模型结果分别计数。
- `cd backend && uv run python scripts/generate_schema_comments.py --check`；新空测试库的 schema 安装和只读 readiness 证据。
- `cd frontend && pnpm check`、`pnpm test`、知识库 mock/真实后端浏览器测试；包脚本中的实际浏览器命令在实施计划中固定。
- 生产镜像构建、能力探测、禁止解析联网的格式矩阵、故障注入与清理演练。

这些是未来验收要求，不是本次规格生成已运行的结果。

## 13. 交付分期与实施前置

四个交付包属于同一设计，按依赖顺序落实；每个包应有独立的实施计划和审阅点，不把所有改动合成一次大替换。

| 交付包 | 可验收产物 | 依赖 |
| --- | --- | --- |
| P1 解析模块与格式能力 | 固定版本移植、三级路由、内容保护、完整来源、本地格式矩阵、离线运行时资源清单；由契约样例验证，不提前切生产摄取 | 依赖可安装性证据 |
| P2 附件与提取结果生命周期 | 新 schema、附件/manifest登记、受控读取、幂等与耐久清理、完整PDF缓存 | P1；M11整合；新空库安装验证 |
| P3 摄取与切分接入 | 真Token切分、Markdown/index_text、preview/ingest一致、CAS发布、reparse/reembed/摘要兼容；启用新摄取路径 | P1+P2 |
| P4 前端与发布验证 | 动态格式、Markdown/图片展示、Token单位、警告、权限/竞态浏览器门、离线镜像门和检索对比 | P3 |

在 P4 全部门禁完成前，不把“解析类已经复制”称为改造完成。不引入半成品的第二个默认摄取入口；最终切换后旧解析器只有明确的旧 profile 兼容用途，没有双路随机降级。

上线前必须已有：用户对本规格的确认、D01 网络范围确认、M11 实施基线、目标部署/数据库处置方案。任一缺失都不授权对现有部署进行破坏性操作。

## 14. 需求追踪

| 用户要求 | 本规格落点 | 明确的适配差异 |
| --- | --- | --- |
| 策略模式 + 三级路由 + list[Document] | 2、3、A01/A02 | 只有file数据源本期实际启用；不新增空壳Notion/网页功能 |
| 专用解析器 + Unstructured分工 | 2.2、9、A08/A24 | 仅本地，不提供API卸载；默认模式保留现有PPTX/EPUB支持 |
| 万物皆Markdown | 3.1、4、A03–A11/A22 | 来源、身份、警告仍结构化；附加index_text用于文本检索 |
| 解析预分段 + Token细分 | 6、A09/A10 | 明确新增真实Token口径，不能使用Dify字符实现冒充 |
| 图片一等公民、持久化、幂等 | 5、7、A12/A15–A20 | 正式摄取持久化；预览无副作用；先完成展示和关系，OCR/图片向量另立范围 |
| 编码探测、降级、PDF缓存、动态白名单 | 5.3、7.3、8、9、A04/A14/A18/A23–A25 | 超时需可终止；缓存保留结构；基础设施失败不静默降级 |

## 15. 核对来源

本节是源码证据索引；目标行为以正文为准，不能反过来把目标 Interface 当作已存在代码。

- 本项目：[根指南](../../../AGENTS.md)、[后端指南](../../../backend/AGENTS.md)、[前端指南](../../../frontend/AGENTS.md)、[领域词汇](../../../CONTEXT.md)。
- 本项目：[提取器](../../../backend/packages/knowledge/actweave_knowledge/ingestion/extractor.py)、[共用预览路径](../../../backend/packages/knowledge/actweave_knowledge/ingestion/preview.py)、[切分器](../../../backend/packages/knowledge/actweave_knowledge/ingestion/splitter.py)、[摄取发布](../../../backend/packages/knowledge/actweave_knowledge/ingestion/pipeline.py)、[重嵌入](../../../backend/packages/knowledge/actweave_knowledge/ingestion/reembed.py)。
- 本项目：[MinIO](../../../backend/packages/knowledge/actweave_knowledge/storage/minio_store.py)、[权限](../../../backend/packages/knowledge/actweave_knowledge/authority.py)、[M11设计](backup/2026-08-31-rag-knowledge-m11-design.md)。
- Dify：[路由](/Users/jiangfeng/dify/api/core/rag/extractor/extract_processor.py)、[默认白名单](/Users/jiangfeng/dify/api/constants/__init__.py)、[实际字符计量](/Users/jiangfeng/dify/api/core/rag/splitter/fixed_text_splitter.py)、[依赖锁文件](/Users/jiangfeng/dify/api/uv.lock)。
- Dify：[Word](/Users/jiangfeng/dify/api/core/rag/extractor/word_extractor.py)、[Excel](/Users/jiangfeng/dify/api/core/rag/extractor/excel_extractor.py)、[Markdown](/Users/jiangfeng/dify/api/core/rag/extractor/markdown_extractor.py)、[PDF](/Users/jiangfeng/dify/api/core/rag/extractor/pdf_extractor.py)、[Unstructured目录](/Users/jiangfeng/dify/api/core/rag/extractor/unstructured/)。
- 用户参考文档：[Dify知识库文件解析机制分析](../../knowledge/Dify知识库文件解析机制分析.md)。其中“默认缓存必然生效”“按Token计数”“Unstructured无法做同类提取”等概括不作为已确认事实。
