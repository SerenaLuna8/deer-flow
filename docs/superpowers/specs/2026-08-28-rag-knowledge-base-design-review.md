# RAG 知识库设计方案 · 开发前最终审查

> 审查对象：`docs/knowledge/` 与 `docs/superpowers/plans/*rag-knowledge*`
> 结论：已按当前项目架构和最小功能范围收口，可进入 M0 开发；代码与配置在对应里程碑实施时修改。

## 1. 最终方案

知识库作为独立软件包：

```text
backend/packages/knowledge/       actweave-knowledge 业务实现
backend/app/knowledge/            宿主 HTTP、Worker、Agent、Secret Adapter
```

依赖方向明确：

- Package 不 import `app.*` 或 `deerflow.*`；
- Harness 不依赖 Package；
- 根应用 composition 同时组装 Package 和 Harness；
- Package 内部数据访问、MinIO 实现和 Embedding/Reranker client 不向宿主泄漏。

## 2. MVP 功能闭环

```text
配置 Embedding + Reranker
  -> 创建 Knowledge Base
  -> 上传 Knowledge Document
  -> Worker 提取/切分/embedding
  -> 写入 Knowledge Segment
  -> pgvector 召回候选
  -> Reranker 精排
  -> Project semantic search
  -> Agent knowledge_search
  -> 前端展示 Knowledge Citation
  -> 重试或删除
```

这条链路在需求、架构、SQL、技术设计和 M0–M7 计划中使用相同对象与状态。

## 3. 最终数据模型

MVP 只有现有 `public` Schema 中的五张表：

```text
knowledge_model_configurations
knowledge_bases
knowledge_documents
knowledge_segments
knowledge_tasks
```

设计选择：

- Document 直接持有 MinIO object key 和切分参数；
- Segment 直接持有正文、来源位置和 embedding；
- 一条 Knowledge Model Configuration 同时保存 Base URL、Embedding model/dimension/batch、Reranker model 和二者共用的当前加密 API Key；
- `knowledge_tasks` 单行保存任务状态、claim、lease、目标 Document version、晚到对象的精确 storage key 和重试计数，是 Knowledge 后台任务的唯一持久状态；
- 当前 API Key 的 nonce/ciphertext 由模型配置行持有，宿主 Adapter 只负责加解密；
- Project 删除前调用 Package `purge_project`。

## 4. 必须保留的功能正确性

### 异步发布

- 用户重试或删除前递增 Document version；
- ingest Task 保存目标 version；
- Worker 发布时同时匹配 Task claim token 和 Document version；
- 写入全部 Segment、Document 转 ready、Task 成功在一个事务完成；
- 迟到任务不得覆盖新 version 或复活已删除 Document。

### 模型与向量

- 被 Knowledge Base 使用的模型配置不能停用，也不能原地修改 Embedding model、Reranker model、base URL 或 dimension；
- embedding 返回数量必须等于输入数量；
- dimension 必须等于配置值；
- NaN/Infinity 等非法值拒绝写入；
- 全零向量拒绝写入和检索；
- 不同模型配置分组生成 query embedding 和执行向量 SQL。
- exact cosine 只召回候选，最终按 Reranker `relevance_score` 排序；
- Reranker 失败时返回明确错误，不静默退回 cosine-only。

### 删除

- Document/Base 在删除开始后不再参与搜索；
- MinIO 对象删除成功后再删除关系行；
- 数据库通过 FK 级联删除 Segment；对应 Knowledge Task 由 Worker 结算为成功；
- MinIO 对象已不存在视为删除成功；
- Project 删除任务重复调用 purge，直到无 Knowledge Base。

这些规则直接防止用户看到错误状态或旧内容，不属于额外平台建设。

## 5. 已移出 MVP 的扩展能力

`RAG检索模块技术设计文档.md` 和 M5 检索计划仍保留。移出的是不会阻塞 MVP 的扩展设计：

- 额外的规范化 JSON 资产和跨语言 golden corpus；
- 独立的模型密钥历史体系；
- 细分的请求权威对象和复杂 readiness 矩阵；
- 内容寻址文件复用、向量缓存和多层清理状态；
- Run 级知识快照；
- 检索账本、模型用量表和维护管理页面；
- 高级搜索、多 Provider 和索引优化。
- 父子分段、语义分段、按 token 分段和可配置分隔符策略。

宿主已有 Project 上下文和 `SecretKey`/`SecretEnvelope` 继续复用，不由 Knowledge Package 重建。

## 6. 实施顺序审查

| 阶段 | 可直接交付的结果 | 阶段门 |
| --- | --- | --- |
| M0 | Package、Interface、KnowledgeSettings | wheel/import/依赖方向 |
| M1 | 五表 Schema、ORM、Repository、默认检索配置 bootstrap | 空库 setup/check/seed |
| M2 | Embedding + Reranker 配置和调用 | mock 双接口 Provider |
| M3 | Base CRUD、MinIO 对象存储与上传 | Base API、对象字节往返、Document+Task |
| M4 | 摄取、任务、重试、删除 | 六格式、迟到任务、purge |
| M5 | exact cosine 候选召回 + Reranker 精排 | 单库/多库/顺序改变/失败不降级 |
| M6 | Agent 工具和 Citation | Worker Run、message replay |
| M7 | 页面和浏览器验收 | Project/Admin/chat E2E |

顺序没有循环依赖：M5 复用 M2/M4，M6 复用 M5，M7 只消费已完成 API。

## 7. 已确认事实与待实施验证

### 已确认

- 仓库使用 PostgreSQL Schema V1 静态安装方式；
- Schema V1 的应用表位于现有 `public` Schema，不为独立 Package 新建数据库 Schema；
- Worker 是后台业务执行者；
- Gateway 是 HTTP 接入层；
- Agent factory 位于 app/Harness 组装路径；
- 独立 Package 可以从根 app 组装而无需建立 Harness 反向依赖。
- 当前唯一 Project 页面壳是 `/projects/[project_slug]/*`，Knowledge 页面继续使用该壳和现有 Project 能力。

### 已冻结的实施基线

- MinIO 是唯一持久文件存储；Gateway 请求 Path 和 Worker 任务 Path 只在单次操作中存在，并在成功、失败或取消后清理；
- 官方同步 MinIO client、临时文件 I/O 和同步 parser 统一通过基于
  `asyncio.to_thread` 的 cancellation-settling adapter 执行；取消后等待已启动调用结束；
- 文件上传强制单 PUT，默认值和配置硬上限均为 50 MiB；每个对象存储实例串行 PUT，
  约束 MinIO SDK 的整 part 内存峰值，并避免 crash 遗留普通对象 list/delete 无法发现的
  incomplete multipart upload；
- MinIO bucket 必须关闭 versioning/Object Lock；启动与删除路径用
  `GetBucketVersioning` 失败关闭，避免 delete marker 被误判为物理清理；
- Knowledge TaskWorker 复用现有 `app.worker` 进程和 stop event；生产/开发 Compose 只透传外部 MinIO 连接配置，不创建新的 MinIO 服务。
- 本机 MinIO 的 S3 API 是 `127.0.0.1:9000`，`http://127.0.0.1:9001` 只用于 Console；bucket 由管理员预先创建，容器运行时改用 Gateway/Worker 均可达的 S3 API 地址。
- 空库安装由代码 bootstrap 写入一条固定的 SiliconFlow Qwen3-VL Embedding + Reranker 配置；SQL 不写模型 seed 或明文密钥，初始化不调用外部 Provider。

### 实施时验证

- pgvector 在目标 PostgreSQL 版本上的无 typmod vector 行为；
- Gateway 上传、Worker 下载和删除同一 MinIO bucket 对象；
- 六种 parser 对产品实际样本文档的提取质量；
- Embedding 与 Reranker Provider 的真实响应格式、dimension、index 和 score；
- Agent ToolMessage Citation 在刷新后的真实 replay。

这些验证分别属于 M1、M3、M4、M2/M5 和 M6，不阻塞 M0 创建软件包。

## 8. 最终判定

**GO：文档基线已对齐现有 public-only Schema V1、根配置、MinIO、现有 Worker 和项目壳，可以从 M0 开始开发。**

放行范围仅是精简后的 MVP，不包含父子分段、语义分段、后续搜索优化、文件去重、缓存、用量分析或模型迁移。实现过程中如果出现新需求，先作为后续项记录，不回填到当前 M0–M7 基线。

## 9. 2026-08-29 开发前增补

进入 M0 前把下列缺口一次性并入基线，`docs/knowledge/` 与 M0–M7 计划已同步修订：

- 检索增加 `score_threshold`：请求可覆盖 0..1，默认包内常量 0.2（M2 对真实 Provider 联调时校准一次），Reranker 精排后过滤，全部低于阈值返回空结果；HTTP 与 Agent 工具使用同一默认值，工具签名不变。
- `knowledge_model_configurations` 增加 `reranker_max_batch`（默认 32）：Rerank 候选分批调用，index 按批内偏移映射，跨批按 `relevance_score` 合并。
- query strip 后长度上限 2000 字符，超限返回 `KNOWLEDGE_INVALID_REQUEST`。
- disabled Base 语义冻结：不参与检索、不接受上传与重试；仍可编辑元数据、读取和删除。
- 新增 Document 原文下载端点（`shared_assets.read`），Gateway 经单次请求临时文件按原始文件名和媒体类型返回。
- 基础配额进入根配置：`max_knowledge_bases_per_project=20`、`max_documents_per_knowledge_base=500`、`max_segments_per_document=5000`；新增错误码 `KNOWLEDGE_QUOTA_EXCEEDED`。
- 切分参数上传后锁定为显式规则；Segment 级停用/编辑、重试调参和 Agent 指定 Base 明确列入后续功能。

判定不变：GO，从 M0 开始开发。
