# RAG 知识库 MVP 执行计划

> 目标：用独立 `actweave-knowledge` 软件包完成可用的上传、摄取、检索和 Agent 引用链路。

## 1. 实施原则

1. 先交付最短可用链路，再增加高级能力。
2. Package 只实现 Knowledge 业务；HTTP、现有鉴权、Worker 生命周期和 Agent 装配留在宿主。
3. 五张 `knowledge_*` 表进入现有 `public` Schema V1，不创建独立 PostgreSQL Schema，也不在 Runtime 建表。
4. 每个里程碑包含聚焦测试，但不建设与当前功能无关的框架。
5. MVP 固定 SiliconFlow `/embeddings`、pgvector exact cosine 候选召回和 `/rerank` 精排。
6. Knowledge 后台任务状态、claim、lease 和 retry 只保存在 `knowledge_tasks`，由现有 Worker 处理。

## 2. 目录

```text
backend/packages/knowledge/
├── pyproject.toml
└── actweave_knowledge/

backend/app/knowledge/
├── bootstrap.py
├── config.py
├── composition.py
├── gateway.py
├── worker.py
├── run_tool.py
└── secret_adapter.py

backend/tests/knowledge/
```

## 3. 里程碑

| 里程碑 | 交付结果 |
| --- | --- |
| M0 | Package 骨架、公开接口、配置和依赖守卫 |
| M1 | 五张业务表、ORM、Schema V1 集成和默认检索模型配置初始化 |
| M2 | Embedding + Reranker 模型配置和 Provider 调用 |
| M3 | MinIO 对象存储、文档上传与原文下载 |
| M4 | 文档解析、切分、embedding、任务与删除 |
| M5 | Project semantic search 与检索测试 API |
| M6 | Agent `knowledge_search` 与 Citation |
| M7 | Project/Admin 页面和端到端验收 |

## 4. 任务拆分

### M0 — 软件包骨架

- 创建 `backend/packages/knowledge/pyproject.toml`。
- 将 Package 加入 backend uv workspace、根应用依赖和 `uv.lock`。
- 创建 `actweave_knowledge` namespace 和 `create_knowledge_module`。
- 定义 `KnowledgeModule`、Settings、DTO、Error 和 `KnowledgeSecretPort`。
- 在根 `config.yaml` 规划 `knowledge.enabled`、worker、upload、配额和 MinIO 配置；由 `backend/app/knowledge/config.py` 从宿主 `AppConfig.model_extra` 读取可选 `knowledge` 映射后使用 Package `KnowledgeSettings` 校验。
- 创建 `backend/app/knowledge/` composition 骨架。
- 增加依赖测试，确保 Package 不 import `app`/`deerflow`。
- 实现阶段在 `config.example.yaml` 保留默认 `enabled=false`，把启用参数写成注释示例，并同步 `README.md`、`Install.md` 以及生产/开发 Compose 的显式 MinIO 环境变量传递；Compose 不创建 MinIO 服务，不把 Knowledge 启动配置加入 System Runtime Settings。

验收：wheel 可安装、根包可 import、composition 可在功能关闭时启动。

### M1 — Schema V1

实现五张表：

```text
knowledge_model_configurations
knowledge_bases
knowledge_documents
knowledge_segments
knowledge_tasks
```

- Package 内实现 ORM 和 PostgreSQL Repository。
- 将 SQL 合并到 `full_schema.sql`。
- 同步 catalog digest、required relations、setup/check/reset 和 Schema 测试。
- 由 `setup-db` 代码 bootstrap 初始化一条固定的 SiliconFlow Qwen3-VL Knowledge Model Configuration；SQL 保持纯 DDL，不写模型记录或 API Key。
- bootstrap 从安装期环境变量取得 API Key，使用现有 `SecretKey`/`SecretEnvelope` 保护后再写库；初始化不调用外部 Provider。
- `reset-db` 在删除 Schema 前完成同样的安装参数预检，Gateway/Worker Runtime 不继承安装期明文变量。
- 管理员预先安装 pgvector；Runtime 只检查。

验收：临时 PostgreSQL 空库执行 setup/check 成功，ORM 与 SQL 一致，并存在一条同时绑定默认 Embedding 与 Reranker 的配置；未提供初始化 API Key 时在 DDL 前失败。

### M2 — Embedding + Reranker 模型配置

- 实现模型配置 CRUD、启停和连接测试。
- 实现 SiliconFlow `/embeddings` 和 `/rerank` 客户端。
- Rerank 候选按配置 `reranker_max_batch` 分批调用并跨批按 `relevance_score` 合并。
- 通过宿主 `KnowledgeSecretPort` 保存/读取当前 API Key。
- 校验 embedding 返回数量、有限数值、非零向量和 dimension。
- 校验 Reranker 返回 index、`relevance_score` 和 `top_n` 映射。
- 配置被 Base 使用后禁止停用，也禁止修改 Embedding model、Reranker model、base URL 和 dimension。
- 提供 Admin 模型配置 API；页面留到 M7。

验收：mock Provider 能完成单条和批量 embedding、候选 rerank 及双接口连接测试；错误能返回稳定业务错误。

### M3 — 文件存储与上传

- 实现 Knowledge Base 创建、列表、详情和更新 API。
- 本机开发环境的 MinIO S3 API 使用 `127.0.0.1:9000`，`http://127.0.0.1:9001` 仅为 Console；应用不得把 9001 当作对象存储 endpoint。
- `actweave-knowledge` bucket 由管理员预先创建；Gateway/Worker 启动检查实际 endpoint 和 bucket 可访问性，不自动建 bucket。
- Compose 内不得使用容器自身的 `127.0.0.1` 访问宿主 MinIO，实施时配置两个进程均可达的 S3 API 地址。
- 实现 Package 内部唯一 `MinioObjectStore`：`upload_from/download_to/delete`，直接包装官方 MinIO client 的 `fput_object/fget_object/remove_object` 并通过 `asyncio.to_thread` 执行。
- 新增上传 API：创建 `uploading` Document、保存文件、置为 `queued`、创建 Task。
- 支持 50 MiB 默认上限和六种扩展名。
- Gateway 把请求写入单次请求临时 Path，上传成功、失败或取消后都清理；上传失败同时清理 MinIO 对象和 `uploading` Document。
- 只有 active Base 接受上传；创建 Base 与上传分别执行 `max_knowledge_bases_per_project` 和 `max_documents_per_knowledge_base` 配额检查。
- 新增原文下载 API：Gateway 经请求临时 Path 从 MinIO 读回，并按原始文件名和媒体类型返回。

验收：上传后能按 Document id 读回相同文件，下载 API 返回一致字节；失败不会创建摄取 Task。

### M4 — 摄取与删除

- 实现 PDF、DOCX、TXT、Markdown、CSV、XLSX extractor。
- Worker 把 MinIO 对象下载到临时 Path 后调用 Extractor，结束后清理临时文件；MinIO I/O 和同步 parser 均通过 `asyncio.to_thread` 执行。
- 实现基础空白清洗、字符切分和 overlap。
- 切分数量超过 `max_segments_per_document` 时摄取失败并记录超限错误。
- 实现 Task claim、超时恢复和最多三次自动尝试。
- Task 直接保存 `target_version`、`claim_token` 和 `lease_until`。
- 在一次事务中写入当前 Document version 的 Segment+embedding，并将 Document 置为 `ready`。
- 发布前检查 Document version 和状态，避免旧任务覆盖重试或删除。
- 实现 Document/Base 删除任务和 `purge_project`。
- 实现当前 Document version 的 Segment 预览 API。
- 把 `purge_project` 接到 Worker 的 Project retention purge 最终数据库清理之前。
- `KnowledgeModule.run_worker(stop_event)` 运行在现有 Worker 进程内，与主 Worker 共享启停和失败生命周期，不新增独立后台服务。

验收：六种 fixture 可处理；Segment 可预览；损坏文件失败后可删除；mock Provider 连续失败至三次耗尽、恢复后用户重试成功；迟到任务不能覆盖新 version；删除后数据库和文件均清理。

### M5 — 检索

- 实现 `KnowledgeSearchRequest/Result/Citation`。
- 只查询 active Base、ready Document 和当前 version Segment。
- 按模型配置分组生成 query embedding。
- 使用 pgvector `<=>` 执行 exact cosine，每组召回 `min(100, max(20, top_k * 5))` 个候选。
- 候选不为空时按 `reranker_max_batch` 分批调用同一配置的 Reranker；按 `relevance_score`、cosine score 和稳定 id 次序排序，过滤低于 `score_threshold` 的候选后返回 top-k。
- Reranker 失败时返回 `KNOWLEDGE_RERANK_FAILED`，不得静默返回 cosine-only 结果。
- 校验 query 长度上限 2000 字符和 `score_threshold` 0..1（默认 0.2）；全部候选低于阈值时返回空结果。
- 提供 Project 检索测试 API。

验收：单库、多库、无命中和不同模型配置分组测试通过；mock Reranker 能改变 cosine 候选顺序，Citation score 等于 `relevance_score`，低于阈值的候选不出现在结果中。

### M6 — Agent 工具

- 在生产 Worker Run 的 Lead Agent 组装路径中按 feature flag 注入 `knowledge_search`。
- 从当前 Run 上下文取得 Project id。
- 工具复用 M5 SearchService，不新增第二套检索实现。
- Tool result 返回 answer context 和结构化 Citation。
- ToolMessage 固定保存 `additional_kwargs.knowledge_citations`，消息投影按 Run 附到最终 Agent 消息并在刷新时恢复。

验收：真实 Worker Run 可调用工具；刷新页面后 Citation 仍显示。

### M7 — 前端

- Project 导航增加 Knowledge。
- 使用现有 `/projects/[project_slug]/*` 项目壳，页面通过当前 Project 取得 UUID；Knowledge query key 使用 account UUID + project UUID，并纳入现有 Project scope 清理。
- 复用 `shared_assets.read/edit/execute` 和 system admin 判断，不新增 Knowledge 能力体系。
- 实现 Base 列表/创建/编辑/删除。
- 实现 Document 上传、列表、状态轮询、重试、删除和原文下载；上传表单说明切分参数上传后不可修改。
- 实现检索测试面板（含可选 `score_threshold`）。
- Admin settings 实现模型配置和连接测试。
- Chat message 实现 Citation 展示。

验收：Playwright 覆盖创建 Base、上传、处理完成、检索、阈值空结果、原文下载、Agent 引用、重试和删除。

## 5. 实施顺序

```text
M0 -> M1 -> M2
            |
            v
           M3 -> M4 -> M5 -> M6 -> M7
```

不并行开发跨里程碑业务逻辑。每个里程碑先合并数据契约和测试，再进入下一个里程碑。

## 6. 开发验收命令

具体命令在实现时接入 Makefile，至少包括：

```text
Package unit tests
Package PostgreSQL tests
MinIO integration tests
backend lint/type/test
make check-db
frontend pnpm check/test
Knowledge Playwright flow
```

只报告本次实际执行的门；mock Provider、临时 PostgreSQL、临时 MinIO 和浏览器测试分别报告，不互相代替。

## 7. MVP 完成定义

同时满足以下条件才算完成：

1. 独立 Package 可安装和测试；
2. 空库可以一次安装五张 Knowledge 表；
3. 六种文件能上传并处理；
4. Project 检索能完成向量候选召回与 Reranker 精排，并返回 Citation；
5. Agent 能调用同一检索服务；
6. 前端可查看状态、重试和删除；
7. Project 删除能清理对应 MinIO 对象和 Knowledge 数据。
