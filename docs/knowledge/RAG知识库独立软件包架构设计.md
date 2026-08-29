# RAG 知识库独立软件包架构设计

> 状态：MVP 开发基线
> 原则：独立软件包、直接实现功能、不提前建设通用平台能力。
>
> 现状：M8（检索与治理增强）后，`knowledge_*` 表由五张扩展到八张（新增
> `knowledge_segment_children`、`knowledge_queries`、`knowledge_metadata_fields`），
> Package 新增 `metadata/` 子模块，Knowledge Base 可通过重建换绑模型配置；
> 第 9 节"后续功能"中父子分段、自定义分隔符、Segment 治理、模型迁移已交付。
> 权威 DDL 见 `backend/packages/harness/deerflow/persistence/full_schema.sql`，
> 交付明细见《RAG知识库MVP执行计划》M8 小节。本文其余内容保持基线原样。

## 1. 架构决策

知识库实现为独立 Python 软件包：

```text
backend/packages/knowledge/
├── pyproject.toml
├── actweave_knowledge/
│   ├── __init__.py
│   ├── module.py
│   ├── contracts.py
│   ├── models/
│   ├── bases/
│   ├── documents/
│   ├── ingestion/
│   ├── retrieval/
│   ├── persistence/
│   ├── storage/
│   └── tasks/

backend/tests/knowledge/
```

- distribution 名称：`actweave-knowledge`。
- import 名称：`actweave_knowledge`。
- Package 是 backend uv workspace member，也是根应用的直接依赖。
- Package 不 import `app.*` 或 `deerflow.*`。
- Package 负责模型配置、Knowledge Base、Knowledge Document、摄取、向量和检索。
- `backend/app/knowledge/` 只负责 HTTP、Worker、Agent 和宿主 Secret 的接入。
- `deerflow-harness` 不依赖 Knowledge Package；根应用在 composition 层同时组装两者。

## 2. 公开接口

根包只公开：

```python
create_knowledge_module(...)
KnowledgeModule
KnowledgeSettings
KnowledgeSecretPort
KnowledgeProtectedSecret
KnowledgeError
KnowledgeModelConfigurationCreate
KnowledgeModelConfigurationUpdate
KnowledgeModelConfigurationView
KnowledgeModelOption
KnowledgeModelConnectionResult
KnowledgeBaseCreate
KnowledgeBaseUpdate
KnowledgeBaseView
KnowledgeDocumentUpload
KnowledgeDocumentView
KnowledgeSegmentView
KnowledgeSearchRequest
KnowledgeSearchResult
KnowledgeCitation
KnowledgeHealth
```

构造入口固定为 `create_knowledge_module(settings=..., session_factory=..., secret_port=...)`；Package 复用宿主 SQLAlchemy session factory，并自行创建 MinIO 对象存储实现与 Embedding/Reranker HTTP client。

`KnowledgeModule` 提供下列功能方法：

```text
create/list/update/delete_model_configuration
list_active_model_options
test_model_configuration
create/list/get/update/delete_knowledge_base
upload/list/get/list_segments/retry/delete/download_document
search
purge_project
run_worker
health
aclose
```

HTTP 请求模型、ORM、MinIO 实现和 Provider 客户端不从根包导出。

## 3. 宿主接入

```text
backend/app/knowledge/
├── bootstrap.py
├── config.py
├── composition.py
├── gateway.py
├── worker.py
├── run_tool.py
└── secret_adapter.py
```

- Gateway 继续使用现有 Project 上下文和能力判断，再调用 Package：读取、列表、Segment 预览、原文下载、`model-options`、health 和检索测试使用 `shared_assets.read`；Base/Document 创建、修改、上传、重试和删除使用 `shared_assets.edit`；Agent Run 调用 `knowledge_search` 使用 `shared_assets.execute`；Knowledge Model Configuration 管理仍要求 system admin。
- `model_configurations` 行持有当前 API Key 的加密 nonce/ciphertext；`secret_adapter.py` 只用宿主现有 `SecretKey`/`SecretEnvelope` 完成加解密。
- 现有 Worker 进程调用 `KnowledgeModule.run_worker(stop_event)` 处理后台任务，与主 Worker 共享启停信号和生命周期，不增加独立 Knowledge 进程。
- `run_tool.py` 把当前 Project id 注入 `knowledge_search` 工具。
- Project 删除流程调用 `purge_project(project_id)`；清理未完成时由宿主删除任务重试。

这些都是宿主适配工作，不进入 Package 的业务模型。

## 4. Package 内部组件

| 组件 | 职责 |
| --- | --- |
| ModelService | 管理一组 SiliconFlow Embedding + Reranker 配置并执行双接口连接测试 |
| KnowledgeBaseService | Knowledge Base CRUD |
| DocumentService | 上传、列表、下载、重试、删除和状态查询 |
| IngestionService | 提取、清洗、切分、批量 embedding 和发布 |
| SearchService | 生成 query embedding、执行 cosine 候选召回、分批调用 Reranker、应用相关度阈值并返回引用 |
| TaskWorker | claim、执行和重试摄取/删除任务 |
| PostgreSQLStore | 五张 Knowledge 表的数据访问 |
| MinioObjectStore | 使用请求临时 Path 上传对象、下载到任务临时 Path 和删除对象 |
| KnowledgeModelClient | SiliconFlow 文本 `/embeddings` 与 `/rerank` 调用，按各自 batch 上限分批 |

## 5. 数据模型

MVP 固定五张表，全部位于现有 `public` Schema：

1. `knowledge_model_configurations`
2. `knowledge_bases`
3. `knowledge_documents`
4. `knowledge_segments`
5. `knowledge_tasks`

一个 Knowledge Model Configuration 行同时保存共享 Base URL、Embedding model、dimension、batch、Reranker model、Reranker batch 和一个共享的当前加密 API Key。处理规则和 MinIO object key 直接保存在 Knowledge Document，embedding 直接保存在 Knowledge Segment。任务 claim、lease 和重试计数直接保存在 Knowledge Task。独立软件包不对应独立 PostgreSQL Schema，不扩展现有 public-only Schema V1 工具链。

## 6. 主要流程

### 6.1 创建 Knowledge Base

1. 选择一个启用的 Knowledge Model Configuration。
2. 保存名称和描述。
3. 同一 Project 内名称不得重复。
4. Project 内 Base 数量不得超过 `max_knowledge_bases_per_project`（默认 20），超限返回 `KNOWLEDGE_QUOTA_EXCEEDED`。

一个 Knowledge Base 固定使用一组 Embedding + Reranker 配置。被 Knowledge Base 引用的配置不能停用，也不能修改 Base URL、Embedding model、Reranker model 或 dimension；需要更换时创建新配置和新 Knowledge Base。

### 6.2 上传与摄取

1. 创建 `uploading` Knowledge Document。
2. Gateway 把请求写入单次请求临时 Path；Package 使用 Document id 生成 object key，并通过 `MinioObjectStore.upload_from` 上传到配置的 MinIO bucket。
3. 更新为 `queued` 并创建 `ingest_document` Task。
4. Worker 把对象下载到任务临时 Path，提取文本、切分并批量调用 embedding，结束后清理临时文件。
5. 在一个数据库事务中替换当前 Document version 的 Segment 和 embedding。
6. Document 更新为 `ready`；失败则更新为 `failed` 并记录可展示错误。

重复上传会创建新的 Knowledge Document，不实现内容去重。

- 只有 active Base 接受上传和重试；disabled Base 仍可编辑元数据、读取和删除。
- Base 内 Document 数量不得超过 `max_documents_per_knowledge_base`（默认 500），超限返回 `KNOWLEDGE_QUOTA_EXCEEDED`。
- 切分参数在上传时一次性固定；调整参数需删除后重新上传（可先下载原始文件）。

### 6.3 重试与重新处理

- `retry_document` 只接受 failed Document，要求所属 Base 处于 active，并沿用原切分参数。
- `retry_document` 递增 Document version，并创建新摄取任务。
- Task 保存目标 Document version。
- Worker 发布前确认 Document 仍是该 version 且没有进入删除状态。
- 同一 Document 同时只允许一个未完成摄取任务。

### 6.4 检索

1. 选择当前 Project 内处于 `active` 的 Knowledge Base。
2. 按 Knowledge Base 的模型配置分组。
3. 每个模型配置生成一次 query embedding。
4. 对相应 Segment 执行 pgvector exact cosine，召回 `candidate_k=min(100, max(20, top_k*5))` 条候选。
5. 候选不为空时，使用同一配置的 Reranker 按 `reranker_max_batch` 分批对 query 和候选文本执行精排。
6. 同一配置内按 Reranker `relevance_score`、cosine score 和稳定 id 排序，丢弃低于 `score_threshold`（请求可覆盖，默认 0.2）的候选；多配置结果再按相同规则合并，返回前 `top_k` 条，未提供时默认 4。全部候选低于阈值时返回空结果。

最终 Citation 的 `score` 是 Reranker 分数。Reranker 失败时整次检索返回明确错误，不退回 cosine-only。不同模型配置的 Reranker score 直接参与最终合并，跨配置校准留到后续。

结果包含：Base id/name、Document id/name、Segment id/position、snippet、score 和来源位置。

### 6.5 Agent 工具

当 Knowledge 功能启用时，宿主为 Agent 注入：

```text
knowledge_search(query, top_k=4)
```

工具始终搜索调用 Run 所属 Project 当前启用的 Knowledge Base。完整结果写入 ToolMessage 的 `additional_kwargs.knowledge_citations`；消息投影按 Run 把它附到最终 Agent 消息，前端据此展示来源。

### 6.6 删除

- 删除 Document：标记 `deleting`，Worker 删除 MinIO 对象，再删除 Document；Segment 和 embedding 由数据库级联删除。
- 删除 Base：标记 `deleting`，Worker 依次删除其 Document 对象和行，最后删除 Base。
- 删除 Task 最终失败时资源保持 `deleting`。没有 open delete Task 时，`KnowledgeBaseView.delete_error` 或 `KnowledgeDocumentView.delete_error` 返回最近失败删除 Task 的错误；用户再次删除创建新 Task 后，处理中 `delete_error=null`。
- 删除 Project：Worker 的 retention purge 在最终数据库清理前调用 `purge_project`；Knowledge 对象和数据未清完时不继续最终 purge。

### 6.7 原文下载

- `download_document` 校验 Document 属于当前 Project 且状态为 `queued|processing|ready|failed`。
- Gateway 提供单次请求临时 Path，Package 通过 `MinioObjectStore.download_to` 写入后返回 Document 视图；Gateway 按原始文件名和媒体类型返回响应，结束后删除临时文件。
- MinIO 对象缺失返回 `KNOWLEDGE_STORAGE_UNAVAILABLE`。

## 7. 后台任务

Knowledge 后台任务状态、claim、lease 和 retry 均以 `public` Schema 中的 `knowledge_tasks` 为唯一持久状态。

Task kind 只有：

```text
ingest_document
delete_document
delete_knowledge_base
```

Task 状态只有：

```text
queued -> running -> succeeded
                  -> retry_wait -> running
                  -> failed
```

- Worker 使用 `FOR UPDATE SKIP LOCKED` claim Task。
- Task 最多执行 3 次。
- `running` lease 过期且仍有剩余次数时进入 `retry_wait`；已用完 3 次时进入 `failed`。
- 删除任务和摄取任务均应幂等。
- 不保存每次 Attempt 的独立历史。
- 请求/任务临时文件读写、MinIO 的 `fput_object`、`fget_object`、`remove_object` 和六类同步 parser 不直接阻塞事件循环；同步调用通过 `asyncio.to_thread` 执行。

## 8. 配置与初始化

### 8.1 Runtime 配置

根 `config.yaml` 默认只需要关闭配置：

```yaml
knowledge:
  enabled: false
```

启用时再配置完整参数：

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

`backend/app/knowledge/config.py` 从宿主 `AppConfig.model_extra` 读取可选的 `knowledge` 映射，再使用 Package 导出的 `KnowledgeSettings` 校验；配置块完全缺失也等同于 `enabled=false`。默认关闭配置不包含 MinIO 环境变量，启用配置才写入并要求完整连接参数。Harness 不 import Knowledge Package，也不把这些启动配置放入 System Runtime Settings。Gateway 与 Worker 使用同一组 MinIO endpoint 和 bucket。MinIO 是 MVP 唯一文件存储实现，不保留本地目录 fallback、存储选择器或多后端字段。模型 endpoint、model 名称、dimension 和 batch size 仍存在数据库模型配置中。

实现 M0/M3 时再同步 `config.example.yaml`、`README.md`、`Install.md` 和生产/开发 Compose 的显式环境变量传递；Compose 不创建 MinIO 服务，endpoint 与 bucket 是部署前提。本轮设计文档不直接修改这些文件。

### 8.2 初始 Knowledge Model Configuration

`make setup-db` 和 `make reset-db` 使用宿主 `backend/app/knowledge/bootstrap.py` 与 Package bootstrap interface 初始化一条确定性配置：

```text
SiliconFlow Qwen3-VL Retrieval
base_url             = https://api.siliconflow.cn/v1
embedding_model      = Qwen/Qwen3-VL-Embedding-8B
embedding_dimension  = 4096
embedding_max_batch  = 64
reranker_model       = Qwen/Qwen3-VL-Reranker-8B
reranker_max_batch   = 32
request_timeout      = 30 seconds
status               = active
```

- 两个模型共用一个 Provider Base URL 和同一个 API Key，因此保存在同一 Knowledge Model Configuration 行，不增加模型类型、配对表或第二份 Secret。
- 首次空库安装和显式 reset 在执行 DDL/删除前要求 installation-only `ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY` 与现有 `ACT_WEAVE_SECRET_KEY` 可用。
- 宿主在 DDL 前使用 `SecretKey`/`SecretEnvelope` 生成 protected material；Package 在五张表创建后、`schema_v1` marker 发布前，以一个事务写入确定性配置。
- `RAG知识库MVP建表.sql` 与最终 `full_schema.sql` 只包含 DDL，不插入模型、密文或明文 Key。
- 初始化不调用外部 Provider；管理员页面连接测试同时验证 `/embeddings` 和 `/rerank`。
- `ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY` 加入安装期环境变量过滤，Gateway、Worker 和 Scheduler 启动时不继承该明文变量。
- 已经完成的 Schema V1 上再次运行 `setup-db` 只读验证，不补写或覆盖模型。实现落地前已有的开发数据库需要按项目既有规则显式重建，或在管理员页面创建相同配置。

### 8.3 当前本机 MinIO

本机只读探测已经确认 endpoint 类型：

```text
S3 API endpoint: 127.0.0.1:9000
Console URL:     http://127.0.0.1:9001
secure:          false
```

- 官方 Python client 的 `endpoint` 填 `127.0.0.1:9000`，不带 `http://`；`9001` 只用于浏览器 Console，不能作为应用 endpoint。
- 目标 bucket 约定为 `actweave-knowledge`，当前未验证是否已经存在。启用前由管理员创建并验证访问；Runtime 不自动创建 bucket。
- 本机提供的用户名和密码在实现配置时分别映射为 `ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY` 与 `ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY`，文档和版本库只保留变量名，不记录明文值。
- Gateway/Worker 直接运行在宿主机时可以使用 `127.0.0.1:9000`。运行在 Compose 容器内时，`127.0.0.1` 指向容器自身；必须改用两个进程都能访问的 S3 API 地址，例如经实际连通性验证后的 `host.docker.internal:9000` 或部署网络内地址。
- `GET /minio/health/live` 成功只证明 MinIO 进程存活。Knowledge health 和 M3 放行必须使用配置凭据验证目标 bucket 可访问，并完成 Gateway 上传、Worker 下载、删除同一 object key 的字节往返。

## 9. 后续功能

以下功能等 MVP 主链路完成后再评估：

- OCR、网页抓取和音视频转写；
- hybrid search、关键词融合和 HNSW；
- 父子分段、语义分段、按 token 分段和自定义分隔符策略；
- Segment 级停用、编辑和单块重建；
- 重试调整切分参数和 Agent 工具指定 Knowledge Base 子集；
- 文档内容去重和 embedding cache；
- 模型迁移和自动重建索引；
- 历史检索回放和用量分析；
- 多 Provider 插件体系。

宿主已有的 Project 上下文和 `SecretKey`/`SecretEnvelope` 继续使用，Knowledge Package 不重复实现。

## 10. 开发放行条件

开发前只要求：

1. Package 路径、依赖方向和公开接口明确；
2. 五张表与状态流一致；
3. 上传、摄取、向量召回、Reranker 精排、Agent 工具和删除流程可直接实现；
4. MinIO、根配置、现有 Worker 和宿主能力映射明确；
5. M0–M7 计划不再引用本节明确不做的能力。
