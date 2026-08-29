# RAG 知识库 MVP 执行计划

> 目标：用独立 `actweave-knowledge` 软件包完成可用的上传、摄取、检索和 Agent 引用链路。

## 1. 实施原则

1. 先交付最短可用链路，再增加高级能力。
2. Package 只实现 Knowledge 业务；HTTP、现有鉴权、Worker 生命周期和 Agent 装配留在宿主。
3. 五张 `knowledge_*` 表进入现有 `public` Schema V1（M8 扩展到八张），不创建独立 PostgreSQL Schema，也不在 Runtime 建表。
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
| M8 | 检索与治理增强（Dify 对齐）：治理、分块质量、检索质量、元数据/重建/新格式 |

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

### M8 — 检索与治理增强（Dify 对齐）

> 状态：已完成（2026-08-30）。以 Dify 1.17.0 知识库模块为参照，分四批把
> 适配现有架构的能力迁移进 `actweave-knowledge`（原独立文档编号 K1–K4，
> 现并入本计划）。`knowledge_*` 表由五张扩展到八张（新增
> `knowledge_segment_children`、`knowledge_queries`、
> `knowledge_metadata_fields`，并扩展 documents/segments/bases 列），
> 已有数据库需重装 Schema（`make reset-db`，破坏性操作）。
> 详细任务拆分见
> `docs/superpowers/plans/2026-08-30-rag-knowledge-m8-retrieval-governance.md`。

实施原则：不引入 Redis/Celery/Unstructured/爬虫服务，异步一律走现有
`knowledge_tasks` 队列，同步预览走 Gateway 请求内计算；Dify 概念到达本仓库
换用本仓库词汇（dataset→Knowledge Base、segment/chunk→Segment、
hit testing→检索测试）；切分参数延续"上传时固化"原则；Schema 改动遵守
ORM、`full_schema.sql`、catalog digest、Schema 测试同批次一起改。

分四批交付：

1. 分段与文档治理：分段启停/编辑/手工新增/删除（编辑同步重算 embedding、
   version 冲突返回 `KNOWLEDGE_CONFLICT`）、文档启停/重命名/批量启停删除、
   分段与文档字数统计；禁用不删向量，重新启用即恢复可检索；
   前端分段浏览页取代预览弹窗。
2. 分块质量：递归分隔符切分（自定义分隔符默认 `\n\n`，回退序列与 Dify
   一致含行边界）、预处理规则（压缩多余空白、删 URL 与邮箱）、
   Gateway 同步分块预览 API（抽取→清洗→切分，不写库不入队），
   创建向导实时预览面板（防抖刷新，所见即所得与实际摄取一致）。
3. 检索质量：父子分块模式（父块承载返回内容、子块承载向量存
   `knowledge_segment_children`，命中按父块内最高子块分回卷去重进精排）、
   库级检索默认参数（top_k 与分数阈值，检索测试与 Agent 工具未传参时生效）、
   查询日志 `knowledge_queries`（来源/结果数/最高分，检索测试页最近查询
   可点击回填）与分段/文档命中计数。
4. 按需扩展（前三项已完成）：
   - 元数据过滤：库级字段定义（string/number/time，存
     `knowledge_metadata_fields`），文档 `doc_metadata` JSONB+GIN；
     字段重命名/删除同事务改写文档键；检索 API 与 Agent 工具支持
     eq/contains/gte/lte 条件（AND 组合，上限 10 条）。
   - 模型重建：`POST /bases/{id}/rebuild` 同步换绑模型配置后逐文档
     version bump 重新入队现有摄取任务；旧版本分段因 version 过滤
     自然退出召回。
   - 新文件格式：`.html/.htm`（BeautifulSoup4）、`.pptx`（python-pptx，
     slide 溯源）、`.epub`（ebooklib，chapter 溯源，跳过导航文档）。
   - URL 单页导入、同名去重：未启动。

暂缓与不做（决策记录）：混合检索（等 zhparser/pg_jieba 评估）、QA 模式与
摘要索引（摄取成本翻倍）、多模态图片段暂缓；Economy 模式/jieba 倒排、
Notion 导入与整站爬取、外部知识库 API、库级 only_me 权限与标签、
文档暂停恢复、归档、RAG Pipeline 工作流摄取不做。

验收（已全部通过）：后端聚焦测试（`tests/knowledge/` 治理/摄取/检索/
元数据/重建套件与 Schema 契约）、前端 `pnpm check` 与单测、mock Playwright
（治理闭环、向导预览、父子模式、最近查询、过滤条件、字段管理、重建确认、
新格式 accept）、real-backend Playwright（编辑后按新内容命中、预览与摄取
逐字节一致、子块命中回卷单一父块引用、库级默认生效、元数据过滤仅命中
匹配文档、重建后新版本可检索）。

## 5. 实施顺序

```text
M0 -> M1 -> M2
            |
            v
           M3 -> M4 -> M5 -> M6 -> M7 -> M8
```

不并行开发跨里程碑业务逻辑。每个里程碑先合并数据契约和测试，再进入下一个里程碑。
M8 内部四批按治理 → 分块 → 检索 → 扩展顺序交付，第三批父子分块依赖第二批的
切分器改造；第四批各项相互独立、按需启动。

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
2. 空库可以一次安装五张 Knowledge 表（M8 后为八张）；
3. 六种文件能上传并处理（M8 后新增 html/htm、pptx、epub）；
4. Project 检索能完成向量候选召回与 Reranker 精排，并返回 Citation；
5. Agent 能调用同一检索服务；
6. 前端可查看状态、重试和删除；
7. Project 删除能清理对应 MinIO 对象和 Knowledge 数据。

M8 为 MVP 之后的增强里程碑，以其小节内的验收条目为完成标准
（已完成，未启动项在小节内明确记录）。
