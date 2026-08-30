# RAG 知识库系统需求文档（MVP）

> 本文只定义用户可见功能和直接实现所需的规则。架构以《RAG知识库独立软件包架构设计》为准。
>
> 现状：本文为 MVP 基线存档。M8（检索与治理增强）已交付下方"不包含"中的
> 部分能力——父子分段、自定义分隔符、Segment 停用/编辑、查询日志与命中统计，
> 以及元数据过滤、模型重建、html/htm/pptx/epub 新格式。M9 已交付：检索
> 模型改由宿主模型注册表管理（Model Provider / Provider Model），
> Knowledge Base 绑定一个 Embedding 模型（换绑走重建）并可选绑定
> Reranker（换绑/解绑即时生效）；无 Reranker 时最终分为余弦相似度
> （`[-1,1]`），有 Reranker 时为其相关性分（`[0,1]`），阈值 0 表示不过滤，
> 召回候选预算为 `min(100, max(20, top_k*5))`。正文的
> `model_configuration_id` 与"知识模型配置"管理界面描述仅作历史存档。
> 当前功能范围以《RAG知识库MVP执行计划》M8/M9 小节为准。

## 1. 产品目标

用户可以在 Project 中：

1. 创建 Knowledge Base；
2. 上传文档并查看处理进度；
3. 对失败文档重试；
4. 测试知识检索；
5. 让 Agent 在回答时搜索当前 Project 的知识库；
6. 查看回答引用的 Knowledge Document；
7. 删除文档或知识库。

管理员可以配置系统使用的 Embedding 与 Reranker 模型组合。

## 2. MVP 范围

### 包含

- 独立 `actweave-knowledge` Python Package；
- SiliconFlow `/embeddings` 与 `/rerank`；
- MinIO 对象存储；
- PDF、DOCX、TXT、Markdown、CSV、XLSX；
- 字符数切分和 overlap；
- pgvector exact cosine 候选召回和 Reranker 精排；
- 检索最低相关度阈值（score threshold）过滤；
- Agent `knowledge_search` 工具；
- Project 页面、模型管理页面和 Knowledge Citation；
- 异步摄取、失败重试和异步删除；
- 原始文件下载；
- Knowledge Base 数量、Document 数量和单文档 Segment 数量的基础配额。

### 不包含

- hybrid search、关键词融合、HNSW；
- 父子分段、语义分段、按 token 分段和自定义分隔符策略；
- Segment 级停用、编辑和单块重建；
- 重试时调整切分参数；
- 网页抓取、OCR、音视频转写；
- 文档内容去重；
- embedding cache；
- 历史检索回放；
- query ledger、模型用量统计和 Knowledge 审计；
- 多 embedding Provider 插件体系。

## 3. 核心对象

### Knowledge Model Configuration

管理员维护的一组检索模型配置。一个配置同时绑定 Embedding 与 Reranker，共用同一个 Provider Base URL 和当前 API Key：

- `display_name`
- `base_url`
- `embedding_model`
- `embedding_dimension`
- `embedding_max_batch`
- `reranker_model`
- `reranker_max_batch`
- `request_timeout_seconds`
- `status=active|disabled`
- 当前 API Key；由宿主 Secret Adapter 加密后保存在该配置行

MVP 只实现 SiliconFlow 的文本 `/embeddings` 和 `/rerank` 契约。模型虽支持多模态，本期摄取和检索仍只发送文本。

首次空库 `make setup-db` 使用确定性 id 初始化一条 active 配置：

```text
display_name         = SiliconFlow Qwen3-VL Retrieval
base_url             = https://api.siliconflow.cn/v1
embedding_model      = Qwen/Qwen3-VL-Embedding-8B
embedding_dimension  = 4096
embedding_max_batch  = 64
reranker_model       = Qwen/Qwen3-VL-Reranker-8B
reranker_max_batch   = 32
request_timeout      = 30 seconds
```

这一行同时初始化两个模型，不建立两种配置行或额外配对关系。`make setup-db` 和 `make reset-db` 在执行 DDL 或删除现有 Schema 之前，从 installation-only 的 `ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY` 读取当前 SiliconFlow Key，并使用宿主 `SecretKey`/`SecretEnvelope` 生成该配置拥有的 nonce/ciphertext。SQL、文档、日志和 Runtime 环境都不保存明文。初始化不调用外部模型接口；连接测试仍由管理员页面完成。

`KnowledgeModelConfigurationView` 还返回派生字段 `in_use`，表示是否已有 Knowledge Base 引用该配置。

### Knowledge Base

Project 内的文档集合：

- `name`
- `description`
- `model_configuration_id`
- `status=active|disabled|deleting`

`KnowledgeBaseView` 还包含 `document_count` 和可选 `delete_error`：没有 open Base 删除 Task 时才从最近一次最终失败的删除 Task 派生。

### Knowledge Document

上传到一个 Knowledge Base 的文件：

- 展示名称和原始文件名；
- MinIO object key 和文件大小；
- 切分长度与 overlap；
- 当前 version；
- 处理状态和错误信息；
- Segment 数量。

`KnowledgeDocumentView` 还包含可选 `delete_error`：没有 open Document 删除 Task 时才从最近一次最终失败的删除 Task 派生。

状态：

```text
uploading -> queued -> processing -> ready
                                -> failed
任意非删除状态 -> deleting -> 物理删除
```

### Knowledge Segment

从一个 Document version 生成的有序文本块，保存正文、位置和来源信息。

### Knowledge Task

后台执行摄取和删除的持久任务。Task 最多尝试 3 次，失败后允许用户再次触发。`delete_document_object` 使用不依赖 Document 外键的精确 `storage_key`，因此可在原 Document 行已消失时继续清理，并可与普通 Document 删除 Task 并存。

### Knowledge Citation

一次检索命中的来源，包含 Base、Document、Segment、snippet、score 和页码/行号等来源位置。

## 4. 功能需求

### FR-01 模型配置

管理员可以：

- 新建模型配置；
- 编辑显示名称、状态、Embedding/Reranker batch size、请求超时和 API Key；
- 测试连接；
- 删除未被 Knowledge Base 使用的配置。

连接测试先用一条短文本调用 Embedding，再用一个 query 和两条候选文本调用 Reranker；两次调用和返回校验都成功才通过。

未被 Knowledge Base 使用时，管理员还可以修改 `base_url`、`embedding_model`、`embedding_dimension` 和 `reranker_model`，保存前必须重新测试两个接口。

当配置已经被 Knowledge Base 使用时，不允许停用，也不允许原地修改 `base_url`、`embedding_model`、`embedding_dimension` 和 `reranker_model`。管理员应创建新配置。

### FR-02 Knowledge Base 管理

Project 用户可以：

- 创建 Base；
- 查看 Base 列表和详情；
- 修改名称、描述和状态；
- 删除 Base。

规则：

- 同一 Project 内 Base 名称唯一；
- 创建时必须选择启用的模型配置；
- Project 内 Base 数量达到 `max_knowledge_bases_per_project`（默认 20）时创建返回 `KNOWLEDGE_QUOTA_EXCEEDED`；
- disabled Base 不参与检索，也不接受上传和文档重试；仍允许编辑名称/描述/状态、读取详情与 Segment、删除 Document 和删除 Base；
- deleting Base 不再接受上传、重试和检索。

### FR-03 上传文档

- 支持单文件上传；批量上传由前端逐文件调用。
- 默认最大文件大小和配置硬上限均为 50 MiB。
- 所有合法文件强制单 PUT，每个对象存储实例只执行一个并发 PUT，以限制 MinIO SDK 的
  整 part 内存，并禁止产生普通对象列表和删除接口不可见的 incomplete multipart upload。
- 允许扩展名：`.pdf`、`.docx`、`.txt`、`.md`、`.csv`、`.xlsx`。
- 每次上传创建新的 Knowledge Document，不做内容去重。
- Gateway 先把请求写入单次请求临时文件并校验大小；Package 生成 MinIO object key，再把临时文件上传到配置的唯一 bucket。
- 请求临时文件在成功、失败或取消后删除，不作为持久文件存储。
- 默认切分长度为 1000 字符，overlap 为 100 字符。
- `overlap` 必须小于切分长度。
- 切分参数按 Document 在上传时设置并一次性固定；重试沿用原参数，调整参数需删除后重新上传（可先下载原始文件）。MVP 只生成一层平铺 Segment。
- 只有 active Base 接受上传；Base 内 Document 数量达到 `max_documents_per_knowledge_base`（默认 500）时上传返回 `KNOWLEDGE_QUOTA_EXCEEDED`。
- 上传期间并发删除必须获胜；即使删除 Worker 先移除 Document 行、put 随后完成且
  即时对象清理失败，也必须保留携带精确 storage key、可与原删除任务并存的
  `delete_document_object` 任务。Base 尚存时恢复 deleting tombstone，使最终错误可见并
  支持用户通过普通删除入口重试，不能产生无行无任务孤儿。

上传完成后立即进入异步摄取，页面显示当前状态。

### FR-04 摄取

摄取顺序固定为：

```text
从 MinIO 下载临时文件 -> 提取文本 -> 基础清洗 -> 切分 -> 批量 embedding -> 发布
```

- TXT/Markdown 首先尝试 UTF-8；失败时尝试 GB18030；有 UTF-16 BOM 时按 BOM 解码。
- PDF 保留页码；CSV/XLSX 保留 sheet 和行号；其他格式来源位置可以为空。
- 空文本进入 `failed`。
- `max_segments_per_document` 是单文档向量条目预算，默认值和可配置硬上限均为 5000：general 模式按 Segment 数量计，parent-child 模式按携带向量的 Knowledge Segment Child 数量计；超限时在调用 Embedding 前进入 `failed` 并说明超限。
- 同一预算适用于摄取和后续手工 Segment 新增/编辑；手工路径在调用 Embedding 前
  检查一次，并在 Provider 返回后持有 Document 锁再次检查，防止并发修改超额。
- embedding 返回数量、维度、有限数值或非零校验不通过时进入 `failed`。
- 成功发布时，同一事务替换该 Document version 的 Segment 和 embedding。
- 旧 version 的任务不得覆盖新 version 或已删除的 Document。
- MinIO 下载和同步 parser 不直接阻塞 Worker 事件循环；处理结束后删除 Worker 临时文件。

### FR-05 失败重试

- 系统任务自动尝试不超过 3 次。
- 摄取自动尝试耗尽后 Document 显示 `failed` 和错误信息。
- 用户点击重试后递增 Document version，并创建新的摄取任务。
- 重试要求所属 Base 处于 active，且沿用原切分参数。
- 重试成功后状态变为 `ready`，旧 Segment 和 embedding 被替换。

### FR-06 检索测试

用户可从 Project Knowledge 页面输入 query，并选择一个或多个 active Base。

- 未选择 Base 时默认搜索当前 Project 全部 active Base；
- query strip 后不能为空，且不超过 2000 字符；
- `top_k` 默认 4，可在请求中覆盖为 1..20；
- `score_threshold` 默认 0.2（包内常量，M2 对真实 Provider 联调时校准一次），可在请求中覆盖为 0..1，0 表示不过滤；
- 每个模型配置只生成一次 query embedding；
- 每个模型配置先用 exact cosine 召回 `candidate_k=min(100, max(20, top_k*5))` 条候选；
- 候选不为空时调用同一配置的 Reranker（候选按 `reranker_max_batch` 分批），按返回 `relevance_score` 精排；
- `score` 表示最终 Reranker 分数，cosine score 只用于候选召回和同分稳定排序；
- 精排后丢弃低于 `score_threshold` 的候选；全部低于阈值时返回空结果；
- Reranker 超时、返回非法 index/score 或 Provider 失败时整次搜索失败，不静默退回 cosine-only；
- 返回结果包含 snippet 与 Knowledge Citation。
- 每次有可搜索 Base 的完成检索把原始 query 记录到当前可信用户自己的历史；
  最近查询只能由同一用户读取，不作为 Project 共享内容向其他成员或管理员展示；
- 搜索在 Provider 工作前后重验证成员关系和 `shared_assets.read`；中途撤权时不得
  返回已经计算出的 Citation，也不得写入该用户的查询历史或命中计数。

### FR-07 Agent 搜索

Knowledge 功能启用时，Lead Agent 获得：

```text
knowledge_search(query: str, top_k: int = 4)
```

- Project id 由宿主当前 Run 上下文提供；
- 查询历史 owner 由同一 Run 的可信用户上下文提供，不暴露为模型工具参数；
- 搜索当前时刻 active 的 Knowledge Base 和 ready Document；
- 不向模型暴露 `score_threshold`，内部使用与检索测试相同的默认阈值；
- 没有命中或全部候选低于阈值时返回空结果，而不是报错；
- 模型或数据库调用失败时返回明确的工具错误；
- 工具成功时把结构化引用写入 ToolMessage 的 `additional_kwargs.knowledge_citations`；
- 消息投影按同一 Run 把引用附到最终 Agent 消息，按 `segment_id` 去重，实时消息和刷新 replay 使用同一逻辑。

### FR-08 删除

- 删除 Document 后不再参与检索，并异步删除其 MinIO 对象、Segment 和 embedding。
- 删除 Base 后不再接受操作，并异步删除其全部 Document 和 MinIO 对象。
- Project 删除前调用 `purge_project` 清理该 Project 的 Knowledge 数据。
- Project purge 先按 Document 行执行对象优先清理，再清扫数据库签发的 Knowledge
  Project 对象 prefix，确保无行晚到对象也被删除，最后删除其余 Knowledge 行；MinIO
  列举或删除失败时不得继续最终 Project 删除。
- Project purge 遇到近期 uploading 行时，本轮不得删除任何 Knowledge 对象或关系行；
  超过一天 settlement grace 的遗留上传仅转为 deleting 并创建 exact-key 清理任务，
  下一轮才执行对象优先清理。时效判断使用 PostgreSQL 时钟；一天远大于正常 MinIO
  传输/重试窗口且远小于 Project 固定 30 天 retention。
- MinIO bucket 必须关闭 versioning 和 Object Lock。启动健康检查以及 MinIO-backed
  Document、Base、Project 删除在删除对象及其关系行前读取 bucket versioning；Enabled、
  Suspended 或缺少 GetBucketVersioning 权限均失败关闭。
- `knowledge.enabled=false` 只停用路由、Agent 工具和 Knowledge Task worker，不停用独立
  Project purger。未配置 MinIO 时，只要仍有 Document 行或状态不是 `succeeded` 的
  `delete_document_object` Task，Project purge 必须返回未完成；纯元数据状态才可直接清理。
- 删除任务失败时自动重试；最终失败时资源保持 `deleting` 并显示 Task 错误。用户再次触发删除后，新 Task 处理期间不再显示旧错误。

### FR-09 页面

Project Knowledge 页面包括：

- Base 列表、创建、编辑和删除；
- Base 详情与 Document 列表；
- 上传入口；
- Document 状态、错误、重试、删除和原文下载；
- 上传表单说明切分参数上传后不可修改；
- 检索测试；
- 处理中的自动刷新。

管理员设置页包括模型配置列表、新建、编辑、连接测试、启停和删除。

聊天消息中的 Citation 可展开显示 Base、Document、snippet、score 和来源位置。

### FR-10 原文下载

- 用户可以下载 Knowledge Document 的原始文件，响应使用原始文件名和记录的媒体类型。
- 仅 `queued|processing|ready|failed` 状态可下载；`uploading` 和 `deleting` 返回 `KNOWLEDGE_INVALID_REQUEST`。
- MinIO 对象缺失时返回 `KNOWLEDGE_STORAGE_UNAVAILABLE`。
- Gateway 先把对象下载到单次请求临时文件再返回，响应结束后删除临时文件。

## 5. API 范围

### Project API

```text
GET/POST   /api/projects/{project_id}/knowledge/bases
GET/PATCH  /api/projects/{project_id}/knowledge/bases/{base_id}
DELETE     /api/projects/{project_id}/knowledge/bases/{base_id}
GET        /api/projects/{project_id}/knowledge/model-options
GET/POST   /api/projects/{project_id}/knowledge/bases/{base_id}/documents
GET        /api/projects/{project_id}/knowledge/documents/{document_id}
GET        /api/projects/{project_id}/knowledge/documents/{document_id}/segments
POST       /api/projects/{project_id}/knowledge/documents/{document_id}/retry
GET        /api/projects/{project_id}/knowledge/documents/{document_id}/download
DELETE     /api/projects/{project_id}/knowledge/documents/{document_id}
POST       /api/projects/{project_id}/knowledge/search
GET        /api/projects/{project_id}/knowledge/health
```

### Admin API

```text
GET/POST   /api/admin/knowledge/models
PATCH      /api/admin/knowledge/models/{configuration_id}
DELETE     /api/admin/knowledge/models/{configuration_id}
POST       /api/admin/knowledge/models/{configuration_id}/test
```

`model-options` 返回 active 配置的 `id`、`display_name`、`embedding_model`、`embedding_dimension` 和 `reranker_model`，供创建 Base 选择。列表 API 使用普通 `page` 和 `page_size`。

### 现有能力映射

- `shared_assets.read`：Knowledge 页面、列表、详情、Segment 预览、原文下载、`model-options`、health 和检索测试；
- `shared_assets.edit`：Base/Document 创建、修改、上传、重试和删除；
- `shared_assets.execute`：Agent Run 注入和调用 `knowledge_search`；
- system admin：Knowledge Model Configuration 管理。

不新增 `knowledge.*` 能力。

## 6. 基础错误码

```text
KNOWLEDGE_DISABLED
KNOWLEDGE_NOT_FOUND
KNOWLEDGE_NAME_CONFLICT
KNOWLEDGE_INVALID_REQUEST
KNOWLEDGE_QUOTA_EXCEEDED
KNOWLEDGE_MODEL_UNAVAILABLE
KNOWLEDGE_STORAGE_UNAVAILABLE
KNOWLEDGE_PARSE_FAILED
KNOWLEDGE_EMBEDDING_FAILED
KNOWLEDGE_RERANK_FAILED
KNOWLEDGE_SEARCH_FAILED
KNOWLEDGE_TASK_FAILED
```

错误响应包含 `code` 和可展示 `message`，不设计字段路径协议或错误摘要协议。

## 7. 验收场景

1. 空库初始化后存在固定的 SiliconFlow Qwen3-VL Knowledge Model Configuration，管理员可以测试或创建新的 Embedding + Reranker 配置。
2. 用户创建 Knowledge Base 并上传六种支持格式。
3. Worker 将文档处理为 `ready`，页面能看到 Segment 数量。
4. 检索测试先召回候选，再由 mock Reranker 改变候选顺序，最终返回按 Reranker score 排序的结果和引用。
5. Agent 调用 `knowledge_search` 并在回答中显示引用。
6. 损坏文件进入 `failed` 后可以删除。
7. mock embedding Provider 持续失败至三次自动尝试耗尽，Provider 恢复后用户重试处理成功。
8. 删除 Document 后搜索不到其内容且 MinIO 对象被删除。
9. 删除 Base 和 Project 后不残留对应 MinIO 对象或 Knowledge 数据。
10. `knowledge.enabled=false` 时不显示 Knowledge 导航，也不向 Agent 注入工具。
11. 与知识库内容无关的 query 在检索测试和 Agent 工具都返回空结果，不产生低相关引用。
12. 用户下载 ready 或 failed Document 得到与上传一致的字节；uploading/deleting Document 不可下载。
13. 超过 Base 数量或 Document 数量配额的创建/上传被拒绝并返回 `KNOWLEDGE_QUOTA_EXCEEDED`。

## 8. 宿主既有约束

Gateway 继续使用现有 Project 上下文和能力判断。Knowledge 的启用、Worker、上传、配额和 MinIO 参数来自根 `config.yaml`，由 `backend/app/knowledge/` 校验后传给 Package，不进入 System Runtime Settings。Knowledge Model Configuration 只保存一个供两个模型共用的当前 API Key 加密值，复用宿主 `SecretKey`/`SecretEnvelope`，不建立独立历史体系。

本机当前已确认 MinIO S3 API 为 `127.0.0.1:9000`，Console 为 `http://127.0.0.1:9001`。程序 endpoint 必须使用 S3 API 的 `host:port`，不能使用 Console 地址。启用前由管理员创建 `actweave-knowledge` bucket；Runtime 不自动建 bucket 或修改其策略。bucket 必须关闭 versioning/Object Lock，凭据必须允许 `GetBucketVersioning`、对象读写删除和 Knowledge Project prefix 列举；Gateway/Worker 启动及所有 MinIO-backed 删除路径据此失败关闭。Gateway/Worker 直接运行在宿主机时可使用 `127.0.0.1:9000`；运行在 Compose 容器内时不能使用容器自身的 `127.0.0.1`，必须配置两个进程都可达的 S3 API 地址。
