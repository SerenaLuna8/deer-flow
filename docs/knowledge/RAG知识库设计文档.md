# RAG 知识库设计文档

> 本文整合自原《RAG知识库独立软件包架构设计》《RAG摄取管道技术设计文档》
> 《RAG检索模块技术设计文档》《RAG模型接入层设计文档》与《RAG知识库MVP执行计划》,
> 描述 M0–M10 全部交付后的当前实现。功能需求见
> [《RAG知识库需求文档》](RAG知识库需求文档.md)。
> 权威 DDL 见 `backend/packages/harness/deerflow/persistence/full_schema.sql`;
> 行为以代码与 `backend/tests/knowledge/` 聚焦测试为准。
>
> 原则:独立软件包、直接实现功能、不提前建设通用平台能力。

## 1. 架构决策

知识库实现为独立 Python 软件包:

```text
backend/packages/knowledge/
├── pyproject.toml
└── actweave_knowledge/
    ├── __init__.py
    ├── module.py
    ├── contracts.py
    ├── authority.py
    ├── models/            # Provider HTTP 客户端(/embeddings、/rerank)
    ├── bases/
    ├── documents/
    ├── metadata/
    ├── segments/
    ├── ingestion/         # extractors/cleaner/splitter/service/progress/reembed
    ├── retrieval/         # service/lexical
    ├── persistence/
    ├── storage/           # MinIO 对象存储
    └── tasks/

backend/app/knowledge/     # 宿主接入
backend/app/model_registry/ # 宿主模型注册表(M9)
backend/tests/knowledge/
```

- distribution 名称:`actweave-knowledge`;import 名称:`actweave_knowledge`。
- Package 是 backend uv workspace member,也是根应用的直接依赖。
- Package 不 import `app.*` 或 `deerflow.*`;`deerflow-harness` 不依赖
  Knowledge Package;根应用在 composition 层同时组装两者。
- Package 负责 Knowledge Base、Document、Segment、元数据、摄取、向量、词法与
  检索;模型配置所有权在宿主模型注册表(M9 起),经注入的 `KnowledgeModelPort`
  取模型材料。
- `backend/app/knowledge/` 只负责 HTTP、Worker、Agent 工具、authority 与
  模型端口的接入。

### Package 内部组件

| 组件 | 职责 |
| --- | --- |
| KnowledgeBaseService | Base CRUD、库级默认参数、检索模式、重排换绑、重建(rebuild)准入 |
| DocumentService | 上传、列表(搜索/过滤/排序/分页)、下载、重试、重解析、重命名、启停、批量操作、删除 |
| MetadataService | 字段定义 CRUD、字段发现、单文档与批量元数据赋值 |
| SegmentService | Segment 启停/编辑/新增/删除、单段详情、字数统计 |
| IngestionService | 提取、清洗、切分、批量 embedding、发布与分块预览 |
| ReembedService(`ingestion/reembed.py`) | 库级重嵌入:保留分段行身份,仅重算向量并整版翻转 |
| SearchService(`retrieval/`) | query embedding、余弦召回、`lexical_v1` 词法召回、RRF、精排、预算、三分支排序、诊断与引用 |
| TaskWorker | claim、执行和重试摄取、重嵌入、资源删除与 exact-key 对象清理任务 |
| ProjectPurger | 独立于功能启停清理 Project 的 MinIO 对象与 Knowledge 行;缺少必要存储配置时失败关闭 |
| PostgreSQLStore(`persistence/`) | 七张 Knowledge 表的数据访问 |
| MinioObjectStore(`storage/`) | 单 PUT 上传、下载到任务临时 Path、校验 bucket 删除语义并执行 exact-key/Project-prefix 删除 |
| KnowledgeModelClient(`models/`) | SiliconFlow 文本 `/embeddings` 与 `/rerank` 调用,按各自 batch 上限分批 |

## 2. 数据模型

当前 Schema V1 中 Knowledge 相关表全部位于现有 `public` Schema:

宿主模型注册表(`app/model_registry/` 持有):

1. `model_providers` — 名称、Base URL、请求超时、行内加密 API Key(供应商级);
2. `model_provider_models` — `model_type ∈ embedding|rerank`、模型名、维度、
   批量上限、`active|disabled`。

Knowledge 七张(`actweave_knowledge` 持有):

3. `knowledge_bases` — `embedding_model_id`(必填)+ `reranker_model_id`
   (可空),库级默认 `top_k`/阈值、`retrieval_mode`;外键只写 SQL 快照;
4. `knowledge_documents` — 展示名/原始名、storage key、8 个冻结切分参数、
   version、状态、`enabled`、字数、`doc_metadata`(JSONB+GIN);
5. `knowledge_metadata_fields` — 库级字段定义(string/number/time);
6. `knowledge_segments` — 正文、位置、来源位置、启停、字数、
   `content_digest`、`lexical_tsv`/`lexical_version`(tsvector+GIN)、
   general 模式的 embedding(parent_child 模式为 NULL);
7. `knowledge_segment_children` — 父子模式子块与其 embedding;
8. `knowledge_queries` — `project_id + owner_user_id` 绑定的原始 query、
   来源、结果数、最高分(`[-1,1]` 或 NULL);
9. `knowledge_tasks` — 任务状态、claim、lease、重试计数、真实进度、
   仅 object-only cleanup 使用的精确 `storage_key`、仅显式重解析使用的冻结
   `reparse_settings`。

处理规则和 MinIO object key 直接保存在 Knowledge Document。独立软件包不对应
独立 PostgreSQL Schema。Schema 变更要求 ORM、`full_schema.sql`、catalog
digest、中文注释与聚焦 Schema 测试同批次一起改;`public.vector`(pgvector)
必须在安装前存在。

原 `knowledge_model_configurations` 表已于 M9 退役。

## 3. 公开接口与宿主接入

### Package 公开接口

根包只公开构造入口、DTO、错误码与常量;HTTP 请求模型、ORM、MinIO 实现和
Provider 客户端不导出。完整清单见 `actweave_knowledge/__init__.py`,分组概览:

- 构造入口:`create_knowledge_module(settings=..., session_factory=...,
  model_port=..., project_active_check=...)`、
  `create_knowledge_project_purger(settings=..., session_factory=...)`、
  不拥有事务的 `purge_knowledge_query_history(session, project_id,
  owner_user_id)`;
- 宿主模型注册表支撑:`create_knowledge_model_client()`(包内 Provider 探活
  客户端的受控构造,类本身不导出,宿主负责 `aclose`)、不拥有事务的
  `retrieval_model_in_use(session, model_id)`(与
  `KnowledgeModule.model_in_use` 同一引用查询,Knowledge 关闭时仍保护
  已被知识库引用的检索模型);
- 模块与端口:`KnowledgeModule`、`KnowledgeSettings`、`KnowledgeModelPort`、
  `KnowledgeEmbeddingMaterial`、`KnowledgeRerankMaterial`、
  `KnowledgeProjectAuthority`、`KnowledgeError`;
- Base/Document/Segment/元数据 DTO:`KnowledgeBaseCreate/Update/View`、
  `KnowledgeDocumentUpload/View`、`KnowledgeSegmentCreate/Update/View/Detail/ChildView`、
  `KnowledgeMetadataFieldView`、`KnowledgeMetadataBatchPatch`、
  `KnowledgeChunkPreviewRequest/Preview`、`KnowledgeReparseRequest/Preview`、
  `KnowledgeRebuildResult`、`KnowledgeTaskProgress/Stage`;
- 检索 DTO:`KnowledgeSearchRequest/Result/Hit`、`KnowledgeCitation`、
  `KnowledgeMetadataFilter`、`KnowledgeSearchDiagnostics/HitDiagnostics/Timings`、
  `KnowledgeScoreKind`、`KnowledgeRetrievalMode`、`KnowledgeEmptyReason`、
  `KnowledgeQueryView`、`KnowledgeHealth`;
- 错误码与预算常量:14 个 `KNOWLEDGE_*` 错误码,以及策略版本、候选预算、
  词法上限、元数据上限等 `KNOWLEDGE_*` 常量。

`KnowledgeModule` 提供 Base/Document/Segment/元数据 CRUD、上传/下载/重试/
重解析/重建、`chunk_preview`、`search`、`get_segment_detail`、
`list_recent_queries`、`purge_project`、`run_worker`、`health` 与 `aclose`。

`create_knowledge_module` 创建完整 MinIO/模型 Runtime;
`create_knowledge_project_purger` 只保留删除历史对象与行所需的最小能力,
不依赖模型端口。`purge_knowledge_query_history` 由宿主 retention Phase B 在
既有 authority 与事务内调用:owner 非空时只清该 owner 的原始查询文本,
`None` 仅用于 Project 全量清理,不级联触碰共享 Knowledge 数据。

### 宿主接入

```text
backend/app/knowledge/
├── authority.py      # 事务内可重验证的 Project authority 适配
├── config.py         # 从 AppConfig.model_extra 读取并用 KnowledgeSettings 校验
├── composition.py    # 组装 Module、独立 purger 与 Worker 注册
├── gateway.py        # Project HTTP 路由
├── worker.py         # 现有 Worker 进程内运行 run_worker(stop_event)
├── run_tool.py       # knowledge_search / knowledge_metadata_fields 注入
└── model_port.py     # KnowledgeModelPort 适配宿主模型注册表
```

检索模型的安装期引导(可选默认 Provider bootstrap)归 `app/model_registry/`
所有。

- Gateway 继续使用现有 Project 上下文和能力判断,再调用 Package:读取类路由
  使用 `shared_assets.read`,写路由使用 `shared_assets.edit`,Agent Run 调用
  使用 `shared_assets.execute`;模型注册表管理要求 system admin,路由并入
  `/api/admin/settings/*`(`app/model_registry/gateway.py`)。
- 每个 Project-facing 读写都把服务端签发的 Project authority 带进事务内重
  验证;请求准入上下文本身不是读权限。
- Knowledge 启用时,现有 Worker 进程调用 `KnowledgeModule.run_worker(stop_event)`
  处理后台任务,与主 Worker 共享启停信号和生命周期,不增加独立 Knowledge
  进程;关闭时不启动该 Task worker,但仍组合独立 Project purger。
- `run_tool.py` 把当前 Project id、Run owner 和事务内可重验证的执行 authority
  注入工具;这些字段都不进入模型可见参数。
- Project 删除流程调用独立 purger 的 `purge_project(project_id)`;清理未完成,
  或 Document 行/未成功 exact-key 清理 Task 存在但删除所需 MinIO 配置缺失时,
  由宿主 retention Job 失败重试,不得继续最终 Project purge。

## 4. 模型接入层

### 所有权与端口

模型配置(供应商、模型、凭据)由宿主模型注册表持有。Package 通过注入的
`KnowledgeModelPort` 取模型材料:

- 端口方法接收调用方 session;建库/重建/换绑按 Provider → Model 取
  `FOR SHARE`,与注册表写路径的 `FOR UPDATE` 串行化;
- `model_in_use(session, model_id)` 只在调用方事务内做非锁定引用查询,
  不反向锁 Project/Base;
- 物化材料分 `KnowledgeEmbeddingMaterial` 与 `KnowledgeRerankMaterial` 两份,
  HTTP 客户端保留在包内供宿主探活复用。

注册表行为语义:模型行所属 Provider、类型、名称、维度建后不可变;被引用
embedding 子模型时 Provider `base_url` 冻结,换端点必须新建 Provider/模型后
显式 rebuild;Key/超时可改,遵循"冻结材料和子模型集合 → 事务外探活 → 重新
锁定复核并提交",不持事务调用模型;被引用模型不可停用/删除,有子模型的
Provider 不可删除。API Key 复用宿主 `SecretKey`/`SecretEnvelope` 行内加密。

### Provider 客户端

实现固定面向 SiliconFlow 文本契约,不建设 Provider 插件注册表:

```text
POST {base_url}/embeddings
POST {base_url}/rerank
```

Embedding 请求(模型名、维度、批量取自模型注册表行):

```json
{
  "model": "Qwen/Qwen3-VL-Embedding-8B",
  "input": ["text 1", "text 2"],
  "dimensions": 4096,
  "encoding_format": "float"
}
```

Reranker 请求:

```json
{
  "model": "Qwen/Qwen3-VL-Reranker-8B",
  "query": "用户问题",
  "documents": ["候选段落一", "候选段落二"],
  "top_n": 4,
  "return_documents": false
}
```

客户端行为:

- Embedding 按模型 `max_batch` 切分输入,恢复 Provider data index 对应的输入
  顺序;
- Rerank 候选按 `max_batch` 分批调用,每批 `top_n = min(top_n, 批内候选数)`,
  返回 index 按批内偏移映射回原候选,跨批按 `relevance_score` 合并;不信任
  返回 document 文本;
- 两个接口使用 Provider 配置的请求超时;对网络错误、429 和 5xx 最多重试 1 次,
  其他 4xx 不重试。

返回校验:

- Embedding:data 数量等于 input 数量;index 可恢复输入顺序;每个向量是数值
  数组、长度等于配置维度、所有值有限且至少一个非零。失败返回
  `KNOWLEDGE_EMBEDDING_FAILED`。
- Reranker:每批 `results` 是数组且数量不超过该批 `top_n`;index 是批内唯一、
  不越界整数;`relevance_score` 是有限数且限定 `[0,1]`;Provider 同分时
  Package 用候选余弦分和稳定 id 完成排序。失败返回 `KNOWLEDGE_RERANK_FAILED`。

直接保存 Provider 返回向量,不额外归一化;cosine distance 由 pgvector 计算。

连接测试按模型类型执行:embedding 模型固定发送一条短文本并确认返回一条指定
维度的合法向量;rerank 模型固定发送一个 query 和两条文本并确认返回合法且可
映射的 index/score。

错误映射:

| 情形 | Error code |
| --- | --- |
| 模型不存在或 disabled | `KNOWLEDGE_MODEL_UNAVAILABLE` |
| 连接或请求超时 | `KNOWLEDGE_MODEL_UNAVAILABLE` |
| Embedding Provider 4xx/5xx 或返回校验失败 | `KNOWLEDGE_EMBEDDING_FAILED` |
| Reranker Provider 4xx/5xx 或返回校验失败 | `KNOWLEDGE_RERANK_FAILED` |

## 5. 摄取管道

完整链路:

```text
upload -> extract -> clean -> split -> embed -> publish
```

### 5.1 上传

支持的提取器:

| 扩展名 | 提取器 |
| --- | --- |
| `.pdf` | pypdf |
| `.docx` | python-docx(正文顺序读取段落及表格行,行内单元格不拆散,合并单元格只计一次) |
| `.txt` / `.md` | 文本解码器 |
| `.csv` | Python csv |
| `.xlsx` | openpyxl |
| `.html` / `.htm` | BeautifulSoup4 |
| `.pptx` | python-pptx |
| `.epub` | ebooklib(跳过导航文档) |

上传流程:

1. Gateway 把请求内容写入单次请求临时 `Path`,同时校验扩展名和大小上限;
2. Package 生成 Document id 和 MinIO object key `storage_key`;
3. 校验 Base 处于 active 且 Document 数量未达配额后,插入 `status='uploading'`
   的 Knowledge Document;
4. `MinioObjectStore.upload_from` 把临时文件上传到配置的 MinIO bucket;
5. 在一个事务中锁定 Document;只有它仍属于同一 Project、保持初始 version 且
   状态仍为 `uploading` 时,才更新为 `queued` 并插入 `ingest_document` Task。
   并发删除通过递增 version 和改为 `deleting` 获胜,上传方清理刚写入的对象且
   不得复活 Document;
6. 上传失败或被并发删除时先删除已写对象,再删除残留 Document。若对象删除失败,
   写入携带精确 `storage_key` 的独立 `delete_document_object` Task;Base 尚存
   时保留或重建 `deleting` tombstone;
7. 无论成功、失败或取消,Gateway 都删除单次请求临时文件。

`MinioObjectStore` 直接包装官方同步 MinIO client:`upload_from`、`download_to`
和 `delete` 分别调用 `fput_object`、`fget_object` 和 `remove_object`,统一通过
可等待同步调用真正结束的 cancellation-settling blocking adapter 执行;取消或
超时不会让后台线程与任务重试重叠。上传获取实例唯一 PUT 槽后重新校验 bucket 仍
未启用 versioning,再把 `part_size` 设为文件大小与 5 MiB 的较大值、并行数设为
1;所有合法文件均为单 PUT,`upload_max_bytes` 默认值和硬上限均为 50 MiB,以
限制 MinIO SDK 的整 part 内存并避免 crash 遗留普通对象列表不可见的 incomplete
multipart。不得提高上限、移除单槽或恢复默认 multipart。

MinIO 是唯一持久文件存储实现,不保留本地目录 fallback 或多后端字段。请求和
Worker 临时 `Path` 只服务于一次操作;Document 只保存 object key。

### 5.2 文本提取

`ExtractedDocument` 由 `ExtractedBlock(text, source_position)` 组成。来源位置:
PDF `{"page": 1}`;XLSX `{"sheet": "Sheet1", "row_start": 1, "row_end": 5}`;
CSV 行号;PPTX slide;EPUB chapter;HTML/DOCX/TXT/Markdown 段落或空位置。

TXT/Markdown 解码顺序:有 UTF-16 BOM 时按 BOM 解码 → UTF-8 → GB18030,都失败
则处理失败。空文件或只含空白字符的文件处理失败。提取执行总字符预算与压缩包
预检。

Worker 为一次处理创建临时目录,把对象下载到临时 `Path` 后调用 Extractor,
处理结束后删除临时目录。MinIO I/O 和同步 parser 均通过 blocking adapter 执行,
不直接阻塞 Worker 事件循环。

### 5.3 清洗与切分

- 基础清洗:统一换行符、去除每行首尾空白、连续三个以上空行压缩为两个、删除
  首尾空白;
- 可选预处理规则(按 Document 冻结):`remove_extra_spaces` 压缩多余空白、
  `remove_urls_emails` 删除 URL 与邮箱;
- 递归分隔符切分:用户分隔符(转义形式,默认 `\n\n`)领衔固定回退序列
  (含行边界),没有合适边界时按字符数切分;输出 position 从 1 连续递增;
- `chunk_size` 默认 1000(200..4000),`chunk_overlap` 默认 100(0..500 且
  小于 `chunk_size`);
- parent_child 模式对每个父块按 `child_chunk_size`/`child_chunk_separator`
  二次切分,只有子块携带向量(父行 embedding 为 NULL);
- `max_segments_per_document` 是单文档向量条目预算(默认与硬上限 5000):
  general 按 Segment 计,parent_child 按 Child 计;超限必须在任何 Embedding
  调用前失败。同一预算适用于手工 Segment 新增/编辑:Embedding 前检查一次,
  Provider 返回后持 Document 锁再次检查;
- 分块预览在 Gateway 请求内同步执行同一条 抽取→清洗→切分 路径:无状态、
  不写行、不入队,临时文件按请求清理,parent_child 模式嵌套返回子块内容,
  与实际摄取所见即所得。

### 5.4 Embedding 与发布事务

1. 经 `KnowledgeModelPort` 物化 Base 绑定的 embedding 材料,按模型批量上限
   分批调用 `embed_many`;每个 Provider 批次前重检查权限与 lease,撤权或失锁
   停止未派发批次;
2. 返回数量、维度、有限数值与非零校验,任一批失败则本次不发布;
3. Worker 完成所有 embedding 后开启短事务:复核并锁定 Project → 锁定 Task 和 Document → 检查
   `claim_token` 与数据库时钟下未过期的 lease → 检查 `version == target_version` → 检查仍是 `processing`
   → 删除旧 Segment → 插入新 Segment(和 Child)并写入向量与词法两列 →
   更新 Document 为 `ready` 并写入 `segment_count`/字数 → Task `succeeded`,
   一次提交;
4. Document 不存在、已 `deleting` 或 version 不匹配时不发布结果,把仍由当前
   claim token 持有的 Task 更新为 `succeeded` no-op,避免重复执行。

`lexical_tsv`/`lexical_version` 与内容写入同事务维护,覆盖发布、重解析、
Segment 编辑/新增与子块重切;重嵌入对词法两列逐字节不动。

Reranker 只参与查询时的候选重排,不参与文档摄取。

## 6. 重新处理

重处理拆分为两个显式入口,加上普通重试:

### 6.1 重试

- `retry_document` 只接受 failed Document,要求所属 Base 处于 active;
- 锁定 Document → `version += 1` → 状态改为 `queued`、清空错误 → 创建新
  索引 Task,`target_version` 为新 version;
- 继承最近一次索引任务的类型与冻结重解析参数;同一 Document 同时只允许一个
  未完成索引任务。

### 6.2 库级重嵌入(rebuild)

`POST /bases/{id}/rebuild` 同步换绑 embedding 模型后,对已发布文档逐个入队
`reembed_document` 任务(准入报告真实接受/跳过计数,未发布文档跳过):

- handler 构造上无 extractor/object store 依赖,不接触原文件与切分;
- 保留 Segment UUID、文本、位置、启停与人工编辑,仅重算向量;
- 一次 version 检查的发布事务整体翻转代次;词法两列逐字节不动。

### 6.3 文档级重解析(reparse)

- `POST /documents/{id}/reparse-preview`:按提交参数从存储的原始文件计算
  服务端预览,只读不写;
- `POST /documents/{id}/reparse`:按 `expected_version` CAS 提交;完整校验的
  8 个切分参数冻结在任务专属 `reparse_settings` 上(任务 kind 复用
  `ingest_document`);发布成功时写回文档行并整体替换全部分段行(人工编辑与
  启停被覆盖)。重解析不是模型变更入口。

索引任务(摄取/重嵌入)按当前 attempt 记录真实进度:阶段、已验证批次计数与
尝试次数,仅当前文档代次投影到 API,失败不显示成功。

## 7. 检索模块

### 7.1 接口与请求规则

```python
@dataclass
class KnowledgeSearchRequest:
    project_id: UUID
    owner_user_id: UUID
    query: str
    knowledge_base_ids: tuple[UUID, ...] | None = None
    top_k: int | None = None
    score_threshold: float | None = None
    source: KnowledgeQuerySource = "retrieval_test"
    metadata_filters: tuple[KnowledgeMetadataFilter, ...] | None = None
    retrieval_mode: KnowledgeRetrievalMode | None = None   # 请求级单次覆盖,不落库
    debug: bool = False
```

- `project_id` 与 `owner_user_id` 由宿主可信请求/Run 上下文提供;HTTP body 和
  Agent 参数只提交 query、可选 Base ids、top_k、过滤与模式覆盖;
- query strip 后非空且 ≤2000 字符;`top_k` 1..20,未提供时用各库级默认
  (缺省 4);`score_threshold` 0..1,0 不过滤(含负分),未提供时用各库级默认;
- 未提供 Base ids 时选择 Project 全部 active Base;提供时忽略
  disabled/deleting;没有可搜索 Base 返回空结果;
- `metadata_filters`(eq/contains/gte/lte,AND,≤10 条)提交前按库内字段
  定义校验,同时约束向量路与词法路。

### 7.2 检索流程

1. 读取目标 Base 并快照每库模型绑定,按
   `(embedding_model_id, reranker_model_id)`(含 NULL)分组;
2. 分库候选预算:全局父段预算 `G=400`,单库预算
   `B=min(100, max(20, top_k*5))`,每库实取 `C=min(B, floor(G/N))`
   (N 为目标库数,`C<1` 显式拒绝);
3. 每个 embedding 模型生成一次 query embedding(跨组复用),校验维度、有限
   数值且非全零;每次分组 Provider 调用前后经宿主 authority 重验证;
4. 向量路:对该组 Base 执行 pgvector exact cosine 召回。general Segment 直接
   携带向量;parent_child 文档经子块召回,按父块内最高子块分回卷去重,一个
   父块一条候选;只查询 active Base、ready 且 enabled 的 Document、当前
   version 且 enabled 的 Segment;不同维度的模型分别执行 SQL;
5. 词法路(hybrid 库):`lexical_v1` 分词(中文双字组、英数词元、业务标识符
   与 IP 规则、字节上限)对 PostgreSQL `tsvector`+GIN 检索;查询词元去重后
   >128 显式拒绝;范围内发现未派生词法行(`lexical_version` 过期)报
   `KNOWLEDGE_CONFLICT`,绝不静默降级;每库按父段 RRF(k=60)合并词法与向量
   两路后再截 C;
6. 精排:有 Reranker 的组把全部入围候选按其批量上限分批调用 `/rerank`,
   相关性分 `[0,1]` 为最终分;Reranker 超时、非法返回或 Provider 失败时整次
   搜索失败,不静默退回 cosine-only。无 Reranker 的组以原始余弦相似度
   `[-1,1]` 为最终分;
7. 按各 Base 阈值过滤(先于全局 top_k),再进入最终排序。

### 7.3 最终排序(三分支)

- 全部目标库同一非空 Reranker,或全无 Reranker 且同一 Embedding:保持原生
  排序,`citation.score` 即原生分(`score_kind=rerank|cosine`);
- 异构分数域:按域内共享名次做秩融合
  `rank_score = 61/2 × 1/(60 + domain_rank)`;词法证据为所有入围父段统一计算
  全局名次并叠加同式词法项;融合分同分只按稳定身份破序,不伪造分差;
  `score_kind=rank_fusion`,原生分保留在 `local_score`;
- 相同 Segment 只返回一次;返回全局前 `top_k` 条;全部候选低于阈值时返回空
  citations。

内部稳定排序键依次为最终分、原生分、Base id、Document id、Segment position、
Segment id。snippet 默认取 Segment 全文的前 320 字符;完整正文按需另行装包。

### 7.4 一致性、诊断与引用

- 检索快照库的模型绑定与本次实际使用的 top_k、阈值、检索模式,在 Provider
  派发前与最终复核时重验;有效策略变化报 `KNOWLEDGE_CONFLICT`,已由请求覆盖
  的库默认值变化不影响本次检索;最终统一复核剔除过期候选并回填真实 `matched_children`;
- 请求级 `debug` 仅在该响应返回安全诊断:策略版本、预算、真实计数、单调耗时、
  模型 ID、逐命中局部分数与四值 `empty_reason`,无任何正文;
- 引用携带 `document_version`/`content_digest` 与 `score_kind`/`local_score`;
  配套单段详情接口(Child 分页,期望版本/digest 漂移报 409);
- Agent 工具在 64KiB UTF-8 JSON 预算内装包完整正文,超出计 `omitted_count`。

### 7.5 服务复用与权限

Project HTTP 检索和 Agent 工具必须复用同一个 `SearchService.search()`;
Embedding 与 Reranker 的 Provider 细节都留在 Package 内部。

检索在每组 query embedding 前、召回事务内、发送 Segment 文本给 Reranker 前、
Provider 工作完成后共四类边界经宿主 authority 重验证;撤权在相应边界抑制后续
Provider 开销、Segment 披露、查询日志、命中计数与已计算 Citation。纯数据库的
查询日志或命中计数写入故障在独立 savepoint 中按 best-effort 处理,不把一次已授权
检索伪装成失败;权限与内容复验、外层事务失败仍必须拒绝返回。

`knowledge_queries` 按 `project_id + owner_user_id` 记录原始 query(来源
`agent|retrieval_test`)。最近查询接口只能读取当前可信 actor 自己的行;
former-owner 和 account 的 retention Phase B 只删精确 Project/owner 的查询
历史,Project 最终清理删除该 Project 全部 owner 的查询历史。

### 7.6 Agent 工具

```text
knowledge_search(query, top_k=None, metadata_filters=None)
knowledge_metadata_fields()
```

工具始终搜索调用 Run 所属 Project 当前启用的 Knowledge Base;`top_k` 省略时
用库级默认;不暴露 `score_threshold`。完整结果写入 ToolMessage 的
`additional_kwargs.knowledge_citations`,stream、values 与 journal 投影均保留,
消息投影按 Run 把引用附到最终 Agent 消息并在刷新时恢复。
`knowledge_metadata_fields` 只返回字段定义,不返回值。没有候选时返回
`{"items": []}`。

## 8. 后台任务

Knowledge 后台任务状态、claim、lease 和 retry 均以 `public` Schema 中的
`knowledge_tasks` 为唯一持久状态。

Task kind:

```text
ingest_document          # 摄取与显式重解析(重解析冻结 reparse_settings)
reembed_document         # 库级重嵌入
delete_document
delete_document_object   # exact-key 对象清理,可与普通删除并存
delete_knowledge_base
```

`INDEXING_TASK_KINDS = (ingest_document, reembed_document)` 统一两种索引任务
的失败派生、过期恢复与重试继承。

Task 状态:

```text
queued -> running -> succeeded
                  -> retry_wait -> running
                  -> failed
```

- Worker 使用 `FOR UPDATE SKIP LOCKED` claim 到期的 `queued|retry_wait`
  Task,设置 `running`、随机 `claim_token`、`lease_until` 并递增
  `attempt_count`;长任务周期性延长 lease;只有仍持有该 token 的 Worker 可以
  提交成功或失败;
- 自动执行最多 3 次:lease 过期且有剩余次数的 `running` 回到 `retry_wait`,
  用完 3 次进入 `failed`;不保存每次 Attempt 的独立历史;
- 只有索引任务最终失败时才把匹配 version 的 Document 置为 `failed`;删除
  Task 失败时,仍存在的 Base/Document/tombstone 保持 `deleting`;没有
  tombstone 的 object-only Task 保留为 Project purge 的失败关闭证据;
- 索引任务行携带真实进度(阶段/已验证批次计数/尝试),仅当前代次投影;
- 删除任务和索引任务均幂等;临时文件读写、MinIO I/O 和同步 parser 通过
  cancellation-settling blocking adapter 执行,取消后等待已启动调用结束再
  释放 claim;
- Project 进入 `pending_deletion` 后,Worker 在 claim 同一事务通过宿主
  Project share-lock callback 检查 active 状态;inactive claim 退回
  `retry_wait` 60 秒且回退本次 attempt,不启动 handler;restore 后自动继续。

## 9. 删除与清理

### Document

1. 标记 `deleting`、递增 version 并清空错误;创建 `delete_document` Task;
2. Worker 删除 MinIO 对象,再删除 Document 行;Segment 由外键级联删除;
3. 若并发上传的晚到 put 在原删除 Task 仍 running 时完成且即时清理失败,创建
   可并存的 `delete_document_object` Task;handler 先验证 exact key 属于可信
   Project/Document,再删除对象和可选 tombstone,绝不靠 prefix 猜测目标。

### Knowledge Base

标记 `deleting` → `delete_knowledge_base` Task → Worker 依次删除其 Document
对象和行 → 删除 Base 行。删除动作可重复调用;MinIO 对象已不存在视为删除成功。
没有 open delete Task 时,View 才从最近失败 Task 派生 `delete_error`;再次删除
创建新 Task 后,处理中 `delete_error=null`。

### Project

Project retention 通过独立 purger(不是 `knowledge_tasks` kind)清理:

1. 恢复该 Project 的过期 Knowledge Task lease,锁定全部 open Task;仍有
   `running` handler 时本轮返回未完成;`queued|retry_wait` 在任何删除前移除;
2. 近期 `uploading` 行使本轮直接返回未完成,期间不删除对象、关系行或执行
   prefix sweep;超过一天 settlement grace 的遗留上传只在本轮转成 `deleting`
   并创建 exact-key Task,下一轮才允许真正清理。判断使用 PostgreSQL 时钟;
   一天显著长于 MinIO 正常传输/重试窗口,且显著短于 Project 固定 30 天
   retention;
3. 按 Document 行执行对象优先清理,再清扫数据库签发的
   `projects/{project_id}/knowledge/` prefix,覆盖无行晚到对象,最后删除剩余
   Knowledge 行;Knowledge 对象和数据未清完时不继续最终 purge。

功能关闭只移除路由、Agent 工具和 Knowledge Task worker,不移除该清理能力;
无 MinIO 配置且仍有 Document 行或未 `succeeded` 的 object-only Task 时失败
关闭;纯元数据状态是唯一不需要 MinIO 的例外。

### MinIO 存储不变量

- bucket 必须保持 versioning `Off`/未配置,不能启用 Object Lock。启动
  health、每次上传获取单槽 PUT 许可后、以及所有 MinIO-backed 删除路径在外部
  I/O 前读取 bucket versioning;`Enabled`、`Suspended` 或缺少
  `GetBucketVersioning` 权限都失败关闭,避免创建不可清除的新版本或让
  `remove_object` 只写 delete marker 后伪成功;
- 凭据必须允许对象读写删除、`GetBucketVersioning` 和 Knowledge Project
  prefix 列举;
- 所有合法上传强制单 PUT(见 §5.1),Project purge 才能证明字节已清除。

## 10. 配置与初始化

### 10.1 Runtime 配置

根 `config.yaml` 默认只需要关闭配置:

```yaml
knowledge:
  enabled: false
```

启用时再配置完整参数:

```yaml
knowledge:
  enabled: true
  worker_concurrency: 2
  task_timeout_seconds: 900
  upload_max_bytes: 52428800
  max_knowledge_bases_per_project: 20
  max_documents_per_knowledge_base: 500
  max_segments_per_document: 5000
  minio:
    endpoint: $ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT
    bucket: actweave-knowledge
    access_key: $ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY
    secret_key: $ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY
    secure: false
```

`backend/app/knowledge/config.py` 从宿主 `AppConfig.model_extra` 读取可选的
`knowledge` 映射,再使用 Package 导出的 `KnowledgeSettings` 校验;配置块完全
缺失等同 `enabled=false`。已写入 Knowledge Document 的部署即使关闭功能,也
必须保留原 endpoint、bucket 和凭据,直到相关 Project retention 与 Document
删除完成;提前移除时 Project purger 返回未完成而非伪成功。Harness 不 import
Knowledge Package,启动配置不进入 System Runtime Settings。Gateway 与 Worker
使用同一组 MinIO endpoint 和 bucket;Compose 不创建 MinIO 服务,endpoint 与
bucket 是部署前提。

### 10.2 初始模型引导

`make setup-db` 与 `make reset-db` 可选地初始化一条默认检索 Model Provider 及
其 Embedding/Reranker 模型(SiliconFlow,`Qwen/Qwen3-VL-Embedding-8B`
4096 维 + `Qwen/Qwen3-VL-Reranker-8B`):

- 安装期环境变量 `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY` 提供当前 Key,
  `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP` 显式跳过;新旧名称都加入
  `backend/scripts/run_runtime.py` 的 installation-only 过滤集合,Gateway/
  Worker/Scheduler 启动不继承明文变量;
- 宿主在 DDL 前用 `SecretKey`/`SecretEnvelope` 生成保护材料;SQL 快照只包含
  DDL,不插入模型、密文或明文 Key;
- 初始化不调用外部 Provider;连接测试由管理员页面完成;
- 已完成 Schema V1 上再次运行 `setup-db` 只读验证,不补写或覆盖模型。

### 10.3 本机 MinIO

```text
S3 API endpoint: 127.0.0.1:9000
Console URL:     http://127.0.0.1:9001
secure:          false
```

- 官方 Python client 的 `endpoint` 填 `127.0.0.1:9000`,不带 `http://`;
  `9001` 只用于浏览器 Console;
- 目标 bucket 约定为 `actweave-knowledge`,启用前由管理员创建并验证访问;
  Runtime 不自动创建 bucket;
- 凭据映射为 `ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY` 与
  `ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY`,版本库只保留变量名;
- Compose 容器内不能使用容器自身的 `127.0.0.1`,必须配置两个进程都可达的
  S3 API 地址;
- `GET /minio/health/live` 成功只证明进程存活;Knowledge health 必须使用配置
  凭据验证目标 bucket 可访问,并完成上传/下载/删除同一 object key 的字节往返。

## 11. 里程碑交付记录

MVP 按 M0→M7 顺序交付最短可用链路,M8–M10 为增强里程碑,全部已完成:

| 里程碑 | 交付结果 |
| --- | --- |
| M0 | Package 骨架、公开接口、配置和依赖守卫 |
| M1 | Knowledge 业务表、ORM、Schema V1 集成与默认检索模型初始化 |
| M2 | Embedding + Reranker 模型配置和 Provider 调用 |
| M3 | MinIO 对象存储、文档上传与原文下载 |
| M4 | 文档解析、切分、embedding、任务与删除 |
| M5 | Project semantic search 与检索测试 API |
| M6 | Agent `knowledge_search` 与 Citation |
| M7 | Project/Admin 页面和端到端验收 |
| M8 | 检索与治理增强(Dify 对齐) |
| M9 | 宿主模型注册表与检索模型拆分 |
| M10 | 检索质量与知识维护工作区 |

### M8 — 检索与治理增强(2026-08-30)

以 Dify 1.17.0 知识库模块为参照,分四批迁移适配现有架构的能力:

1. 分段与文档治理:Segment 启停/编辑/手工新增/删除(编辑同步重算 embedding、
   version 冲突返回 `KNOWLEDGE_CONFLICT`)、文档启停/重命名/批量启停删除、
   字数统计;禁用不删向量;
2. 分块质量:递归分隔符切分(自定义分隔符默认 `\n\n`)、预处理规则、Gateway
   同步分块预览、创建向导实时预览;
3. 检索质量:父子分块模式、库级检索默认参数、查询日志 `knowledge_queries`
   与分段/文档命中计数;
4. 元数据过滤(字段定义 + `doc_metadata` JSONB+GIN + eq/contains/gte/lte)、
   模型重建入口、新格式 `.html/.htm`/`.pptx`/`.epub`。

`knowledge_*` 表由五张扩展到八张。决策记录:混合检索当时暂缓(M10 落地)、
QA 模式/摘要索引/多模态暂缓;Economy 模式、Notion 导入与整站爬取、外部知识库
API、库级 only_me 权限与标签、文档暂停恢复、归档、Pipeline 工作流摄取不做。

### M9 — 模型注册表与检索模型拆分(2026-08-30)

- 模型配置所有权与 seed 迁至宿主模型注册表
  (`model_providers`/`model_provider_models`):供应商级凭据、
  Embedding/Reranker 类型化拆分、rerank 库级可选;
- `knowledge_bases.model_configuration_id` 改为 `embedding_model_id`(必填)+
  `reranker_model_id`(可空);`knowledge_model_configurations` 退役,
  `knowledge_*` 由八张变七张;
- Package 让出模型配置所有权:删除 models CRUD、`KnowledgeSecretPort` 与包内
  seed,新增 `KnowledgeModelPort`;HTTP 客户端拆两份物化材料保留在包内;
- 行为语义:换绑/解绑 rerank 即时生效不重建;无 rerank 最终分为余弦
  `[-1,1]`,有 rerank 为 `[0,1]`;换 embedding 走 rebuild;候选预算按
  `(embedding_model_id, reranker_model_id)` 独立分配;
- 引导环境变量改名为 `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY`/`_SKIP`;
  Admin 路由并入 `/api/admin/settings/*`;
- 同批完成 DeepSeek 单入口收敛(唯一适配器 `deepseek`)与 OpenAI 双协议入口
  (`openai` 固定 Chat Completions、`openai_responses` 固定 Responses),
  删除 `patched_openai`/`patched_deepseek`;计量 revision v6→v7。
- M9 不提供旧数据迁移路径,按空库重新初始化交付(操作者确认后
  `make reset-db`)。

> 补记(2026-08-31 模型供应商统一):M9 交付时注册表仅服务检索模型,上述
> 记录保留当时事实。此后 `model_providers` 升级为整个模型域的唯一凭据与端点
> 所有者:每个 System Model(文本模型)绑定必填 `provider_id`,`base_url`
> 由供应商派生,模型级 API Key 与清除入口删除;供应商 Key/端点变更在同一
> 事务内对全部绑定文本模型做 fan-out 重加密(锁竞争回滚整个 settle 并返回
> 409)。注册表管理与 Knowledge 模块启用状态解耦(Gateway lifespan 提供
> 探活客户端);DeepSeek 引导 Key 落为 DeepSeek 供应商,与 SiliconFlow seed
> 各用各的 Key。Provider Model 词义不变,仍专指 Embedding/Reranker,不因
> 管理页按供应商分组而混同文本模型。

### M10 — 检索质量与知识维护工作区(2026-08-31)

十项增量:完整 Segment 正文供模型使用(64KiB 预算);重嵌入与重解析两个显式
重处理入口;创建向导预览隔离参数变化与迟到响应;文档列表搜索/过滤/排序/
分页;安全深链接与 Segment 定位;检索分数来源、真实命中 Child 与诊断;真实
任务进度;元数据字段发现与批量赋值;PostgreSQL 词法派生索引与显式 hybrid
召回;分库候选预算与分数域排序。

真实质量门(SiliconFlow 真实模型,65 题冻结语料、10002 检索单元):标识符
候选/最终 Recall 1.0(门槛 ≥0.95),自然语言召回/nDCG 相对语义基线零回归,
无答案误召回 0;词法路非 Provider P95 增幅(约 5.4×)按方案记录并接受。
开发目标库已按 M10 Schema V1 重置并通过 `make check-db`。

## 12. 测试与验收门

- 后端聚焦测试位于 `backend/tests/knowledge/`(治理/摄取/检索/词法/元数据/
  重建/重嵌入/进度/契约与质量评测套件),需要开发 PostgreSQL 与本机 MinIO;
- Schema 契约:ORM、`full_schema.sql`、catalog digest、中文注释与 Schema
  测试同批验证;
- 前端 `pnpm check` 与单测;mock Playwright 与 real-backend Playwright 分开
  报告,Knowledge 用例不得因引导失败而 skip;
- 开发验收命令(接入 Makefile):Package 单测、Package PostgreSQL 测试、
  MinIO 集成测试、backend lint/type/test、`make check-db`、frontend
  `pnpm check`/test、Knowledge Playwright flow;
- 只报告本次实际执行的门;mock Provider、临时 PostgreSQL、临时 MinIO 和
  浏览器测试分别报告,不互相代替;真实模型质量评测证据单独报告。
