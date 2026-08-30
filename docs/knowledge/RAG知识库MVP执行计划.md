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
| M9 | 模型注册表（已交付）：供应商级凭据、Embedding/Reranker 拆分、rerank 可选、管理端合并、DeepSeek 收敛、OpenAI 双协议入口与补丁退役 |
| M10 | 检索质量与知识维护工作区（计划中）：完整模型正文、安全重处理、文档定位与检索诊断、真实进度、元数据批量维护、混合召回与多库排序 |

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
- `actweave-knowledge` bucket 由管理员预先创建；Gateway/Worker 启动检查实际 endpoint、bucket 可访问性和 versioning 为未配置/`Off`，不自动建 bucket 或修改 bucket 策略。
- Compose 内不得使用容器自身的 `127.0.0.1` 访问宿主 MinIO，实施时配置两个进程均可达的 S3 API 地址。
- 实现 Package 内部唯一 `MinioObjectStore`：`upload_from/download_to/delete`，直接包装官方 MinIO client 的 `fput_object/fget_object/remove_object` 并通过 cancellation-settling blocking adapter 执行。
- 新增上传 API：创建 `uploading` Document、保存文件，并仅在初始 version 和 `uploading` 状态仍成立时置为 `queued`、创建 Task；并发删除获胜且上传完成后不得复活 Document。删除 Worker 先删行、put 后完成且请求内对象清理失败的组合路径必须留下携带精确 storage key 的独立 `delete_document_object` Task，并在 Base 尚存时恢复 deleting tombstone。Project purge 对近期 uploading 本轮失败关闭；超过一天的遗留上传先转 deleting/入队并延迟一轮，再按可信 prefix 兜底清扫无行对象。
- MinIO bucket 仅支持 versioning/Object Lock 关闭状态；启动、每次上传取得单槽 PUT 许可后和所有 MinIO-backed 删除路径必须先读取 versioning，凭据需要 GetBucketVersioning、prefix list 和 object delete 权限。
- 所有上传强制单 PUT；`upload_max_bytes` 默认值和硬上限均为 50 MiB，每个 `MinioObjectStore` 串行 `fput_object`，限制 SDK 整 part 内存并避免崩溃遗留普通 prefix sweep 不可见的 incomplete multipart upload。
- 支持 50 MiB 默认上限和六种扩展名。
- Gateway 把请求写入单次请求临时 Path，上传成功、失败或取消后都清理；上传失败先清理 MinIO 对象，成功后才删除残留 Document；对象删除失败则保留 exact-key Task 和可选 tombstone。
- 只有 active Base 接受上传；创建 Base 与上传分别执行 `max_knowledge_bases_per_project` 和 `max_documents_per_knowledge_base` 配额检查。
- 新增原文下载 API：Gateway 经请求临时 Path 从 MinIO 读回，并按原始文件名和媒体类型返回。

验收：上传后能按 Document id 读回相同文件，下载 API 返回一致字节；失败不会创建摄取 Task。

### M4 — 摄取与删除

- 实现 PDF、DOCX、TXT、Markdown、CSV、XLSX extractor。
- Worker 把 MinIO 对象下载到临时 Path 后调用 Extractor，结束后清理临时文件；MinIO I/O 和同步 parser 均通过 cancellation-settling blocking adapter 执行。
- 实现基础空白清洗、字符切分和 overlap。
- `max_segments_per_document` 作为不可配置超过 5000 的单文档向量条目预算：general 按 Segment 计数，parent-child 按 Knowledge Segment Child 计数；超限时在调用 Embedding 前失败并记录超限错误。
- 实现 Task claim、超时恢复和最多三次自动尝试。
- Task 直接保存 `target_version`、`claim_token`、`lease_until`，并仅为
  `delete_document_object` 保存精确 `storage_key`。
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
   version 冲突返回 `KNOWLEDGE_CONFLICT`，手工新增/编辑在 Embedding 前与最终
   Document 锁内都重算单文档向量条目预算）、文档启停/重命名/批量启停删除、
   分段与文档字数统计；禁用不删向量，重新启用即恢复可检索；
   前端分段浏览页取代预览弹窗。
2. 分块质量：递归分隔符切分（自定义分隔符默认 `\n\n`，回退序列与 Dify
   一致含行边界）、预处理规则（压缩多余空白、删 URL 与邮箱）、
   Gateway 同步分块预览 API（抽取→清洗→切分，不写库不入队），
   创建向导实时预览面板（防抖刷新，所见即所得与实际摄取一致）。
3. 检索质量：父子分块模式（父块承载返回内容、子块承载向量存
   `knowledge_segment_children`，命中按父块内最高子块分回卷去重进精排）、
   库级检索默认参数（top_k 与分数阈值，检索测试与 Agent 工具未传参时生效）、
   查询日志 `knowledge_queries`（可信 owner、来源/结果数/最高分；检索测试页只列
   当前用户自己的最近查询并可点击回填）与分段/文档命中计数；Provider 工作完成
   后再次重验证权限，中途撤权不得返回结果或写查询历史。
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

### M9 — 模型注册表与检索模型拆分（B1 + DeepSeek/OpenAI 适配器精简）

> 状态：已交付（2026-08-30 立项，同日审查修订并实施完成）。动机：管理端"模型设置/知识
> 模型"双菜单并存；一条知识模型配置把 Embedding 与 Reranker 捆绑在同一
> 行、共用同一把 API Key——跨供应商组合不可能、重排被迫强制、组合爆炸
> 伴随密钥副本、只换重排也要全量重嵌入。对齐 Dify 的信息架构（供应商级
> 凭据 + 类型化模型 + 消费侧独立选择），不引入其插件体系。本次另纳入
> DeepSeek 单入口/单实现收敛，以及“OpenAI 兼容（Chat Completions）”/
> “OpenAI Responses”两个固定协议入口，共用原生 SDK 并删除 `patched_openai`；
> 不将签名补丁迁入其他实现。LLM 的 `system_model_configs` 不迁入新
> 注册表，快照校验与密钥世代机制仍保留。用户已确认 M9 可以重置数据库，
> 因此按重新初始化交付，不保留旧适配器别名或旧 checkpoint 兼容。
> 已审查：LLM 消费点均走 system
> 目录/快照/策略链，与知识零耦合；全库 pgvector 仅知识两表，无其他
> embedding 消费者。LLM 目录整体整合仍留 B2。任务拆分见
> `docs/superpowers/plans/2026-08-30-rag-knowledge-m9-model-registry.md`。

数据模型：宿主新增 `model_providers`（名称、base_url、超时、行内加密
Key，B1 无整体停用状态）与 `model_provider_models`（model_type ∈
embedding|rerank、模型名、维度、批量、active|disabled）；
`knowledge_bases.model_configuration_id` 改为
`embedding_model_id`（必填）+ `reranker_model_id`（可空），外键只写 SQL
快照（沿用 projects 先例）；`knowledge_model_configurations` 退役，
`knowledge_*` 由八张变七张；`knowledge_queries.top_score` 约束放宽为
`[-1,1]` 或 NULL，同步 ORM、SQL、digest、注释与测试。M9 不提供旧数据
迁移路径；已有数据库需由操作者确认准确目标、停服和数据处置后执行
`make reset-db`。该命令清理整个应用 `public` Schema，不仅是 Knowledge，
旧模型、密钥世代、Run 与 checkpoint 不作为 M9 可恢复数据；本次计划
修订不执行命令，reset 不得隐藏在普通启动中。MinIO 文件不随 SQL reset
自动删除，其保留/清理另行确认。

包边界：`actweave_knowledge` 让出模型配置所有权——删除 models CRUD、
`KnowledgeSecretPort` 与包内 seed；新增 `KnowledgeModelPort`（端口方法
接收调用方 session，建库/重建/换绑按 Provider → Model 取 FOR SHARE，
与注册表写路径的 FOR UPDATE 串行化）；`model_in_use(session, model_id)`
只在调用方事务内做非锁定引用查询，不反向锁 Project/Base。HTTP 客户端
拆 Embedding/Rerank 两份物化材料，保留在包内供宿主探活复用；构造器与
工厂继续注入 `project_active_check`，保留任务认领时的删除/恢复保护和
功能关闭时仍独立装配的 Project purge，不因替换模型端口而删减这些能力。

行为语义：reranker 为库级可选设置，换绑/关闭即时生效不重建；无 rerank
时最终分＝原始余弦 `[-1,1]`，rerank 明确限定 `[0,1]` 并补客户端越界
校验；阈值仍 `[0,1]` 且 0=不过滤（含负分），日志与最终引用分数同源。
Query embedding 按 embedding 模型复用，候选预算按 `(embedding_model_id,
reranker_model_id)`（含 NULL）独立分配；重排对组内全部候选评分，先按
各 Base 阈值过滤，再稳定排序/去重/取全局 top_k。跨模型/余弦原始分混排
仍是 B1 接受的质量限制，不宣称已校准，不增加评分融合框架。

换 embedding 才走 rebuild（version bump 重入队）；模型行所属 Provider、
type/名称/维度建后不可变。有被引用 embedding 子模型时 Provider 的
`base_url` 冻结，换端点必须新建 Provider/模型后显式 rebuild，不能凭
同名、同维度、探活成功绕过；允许的地址更新仍须重新提交 API Key。
Key/超时可改，但遵守“冻结材料和子模型集合 → 事务外探活 → 重新锁定复核
并提交”，不持事务调用模型、不用旧探活结论覆盖并发更新。被引用模型
不可停用/删除，有子模型的 Provider 不可删除；注册表与 knowledge 同门控，
Admin 路由并入 `/api/admin/settings/*`，沿用系统审计上下文。

引导改名覆盖 setup/check/reset、Compose、Install 及 `run_runtime.py`：
新 `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY`/`ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP`
和残留旧名称均须被运行时过滤，分别测试 `.env` 与父进程来源。replay seed
和 Gateway 启动调用同批迁为 Provider＋独立模型，不再导入退役模块。

前端覆盖统一模型管理、创建向导、“创建空知识库”独立对话框、库设置与
检索测试页；无 rerank 不阻止建库，Provider 无停用开关，被引用 embedding
冻结地址但不冻结 Key/超时编辑。检索及历史统一使用中性分数标签，说明
余弦/重排范围和 0 不过滤，不按当前库设置反推历史分数来源，不新增
score_kind；更换重排后清除旧搜索结果。

DeepSeek 专项（Task 9）：按 [官方思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)
与 [多轮对话指南](https://api-docs.deepseek.com/zh-cn/guides/multi_round_chat/)，
保留 patched 实现的历史 reasoning 完整回传：带 tools 时必需（即使没有
实际 tool_calls），不带 tools 时传入会被忽略，不新增 tools 条件分支。
唯一适配器 ID 为 `deepseek`，直接使用保留完整回注的 `PatchedChatDeepSeek`
实现；删除 `patched_deepseek` 描述器与运行时标识分支，不保留 alias。
管理端创建/编辑均只显示一个“DeepSeek”并提交 `deepseek`，无需旧记录
特殊处理。默认三个模型在新库统一按 `deepseek` seed，模型 UUID/名称及
Flash/Pro/Vision 独立身份不变；安装时按新 ID 重新生成 checksum、Secret
recipient 和加密世代，不直接改旧行或复用旧密文。DeepSeek 引导 Key 与
检索 Provider 引导 Key 仍独立，thinking/Run 模式/effort 映射不变。

DeepSeek 收敛须同改 `provider_wire.py`，使实际请求、Context lane、Profile、
cost fingerprint 和压缩容量估计一致；两个共享计量 revision 从当前 v6
递增到 v7。旧 v6 checkpoint 随数据库 reset 清理，不编写旧 Profile
重冻结或双版本兼容机制。新 Run 从新 revision 建立 Profile，仍验收
同版本普通/人工 `Command` 恢复和其他 Provider 计量；旧/未知版本、坏
指纹仍拒绝，保留现有 payload/checksum 和密钥校验，不放宽恢复权限。

OpenAI 专项（Task 10）：按 [官方协议说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)
提供两个公开描述器，共用 `langchain_openai:ChatOpenAI`，不复制客户端：

- “OpenAI 兼容（Chat Completions）”：ID `openai`，固定
  `use_responses_api=false`，只调用 `{base_url}/chat/completions`，请求 messages。
- “OpenAI Responses”：ID `openai_responses`，固定
  `use_responses_api=true` / `output_version="responses/v1"`，只调用
  `{base_url}/responses`，请求 input items。

两者 Base URL 均可配置，端点必须支持所选协议；不根据模型名/URL猜测，
不在失败后跨协议 fallback。协议字段由后端派生，不再提供独立布尔开关
或 Output version 下拉；authoring、探活、冻结材料物化与最终 factory
都验证选择一致，拒绝手写协议字段/冲突覆盖，不能仅设置可被覆盖的默认值。
固定字段仅在验证后输出 ModelConfig 时派生，不写持久化 settings/Admin DTO
或增加 Run 快照字段；编辑往返仍只有业务参数，避免隐藏字段造成契约冲突。
协议 ID 随 System Model/Run payload 冻结并参与现有密钥绑定，切换入口
须按改适配器流程重新提交 Key；同入口普通编辑仍可留空保留。

`openai_responses` 从内部计量分类成为可选择的协议 ID，wire/Profile/
fingerprint/outcome/vision 必须与最终 SDK 请求一致，不能因共用类名误判
或重复追加后缀。直接删除 `patched_openai`/`PatchedChatOpenAI` 与
`patched_openai_responses` 专属分支，不保留旧兼容、不移植签名补丁；
DeepSeek 的公共恢复 helper 和 vLLM 自身回放保留，与 Task 9 共用 M9
计量 revision 提升。

Responses 补齐标准输出/流式工具回放及已返回 summary 的识别、计时和
前端“推理摘要”展示，保留原始 AIMessage 和工具关联，支持历史刷新；
不展示/解释 encrypted_content，不构造完整思维链。仍使用应用侧历史，
不自动引入 previous_response_id、服务端 Conversations、内置搜索或新 MCP
执行能力；双协议入口不等于扩大工具授权。

实施分三个内部阶段：① 准备契约与回归，先补新增客户端/端口再接注册表
② 功能分支内原子切换 Schema、包、宿主、引导/replay 与前端，Task 9/10
与语言模型表单同阶段联调，最后删旧 RAG 契约、`patched_deepseek` 标识和
`patched_openai` 实现/标识 ③ Task 11 完整验收和文档更新后整体合并。
Task 1–11 是工作项而非独立发布批次；不先合并破坏性
删表，不单独发布不兼容的后端/前端契约，也不增加长期双写或旧数据兼容层。

验收（计划）：后端 `tests/model_registry/` 与包套件、Schema 契约与脚本
测试、运行环境安全及 replay 引导测试；重点覆盖端点冻结/绑定竞争、
stale probe、负分日志与命中计数、候选预算、阈值先于 top_k、Worker
删除/恢复。mock Playwright 覆盖模型管理、分型探活、引用保护、向导与
空库两个入口、关闭重排、负分与设置切换、重建确认及导航合并；
real-backend Playwright 验证换 rerank 不重嵌、关闭 rerank 仍可检索、
重建后新版本可检索，隔离 query embedding 对计数断言的影响。mock 与
real-backend 分开报告，Knowledge 用例不得因引导失败而 skip。
还需通过 DeepSeek 流式/非流式、跨轮工具 reasoning、统一 wire/计量、
重新 seed/密钥解密/新 Run 准入及同版本恢复、管理端单一描述器入口回归，
确认旧标识不再受支持；不要求旧数据兼容验收。mock 与真实 DeepSeek
联通证据分开报告。OpenAI 另验两个描述器对应各自 HTTP 路径/body、冲突
字段拒绝/无自动协议切换、工具多轮/流式/图像/summary 与历史刷新、计量
一致及新 Run 恢复；补丁模块无生产引用，不再验收自定义签名
回传能力。完成实施
后同步 CONTEXT.md、架构/模型/检索/需求设计、README/Install 和两份
AGENTS.md；本次计划修订不提前改写当前实现事实或标记已完成。

### M10 — 检索质量与知识维护工作区（计划中）

详细规范：[M10 设计方案](../superpowers/specs/2026-08-30-rag-knowledge-m10-quality-workbench-design.md)。
执行拆分：[M10 执行计划](../superpowers/plans/2026-08-30-rag-knowledge-m10-quality-workbench.md)。

前置为 M9 前后端契约切换和完整验收，不把当前工作树中的 M9 修改视为已完成。
M10 参考 Dify 的功能和操作逻辑，在现有 Knowledge Package、Project authority、
Worker、PostgreSQL 与 MinIO 内实现，不复制 Dify 代码/资产或引入平行平台。

本期十项增量：

1. 完整 Segment 正文供模型使用，短 Citation 用于展示；以64KiB UTF-8正文预算整段选择。
2. 库级重嵌入保留人工内容、UUID、启停和历史；文档原文件重新解析单独预览、确认和执行。
3. 创建向导选择预览文件，并隔离参数变化与迟到响应。
4. 文档关键词、状态筛选、排序和完整列表分页。
5. 安全深链接、Segment定位和返回恢复，不将业务内容写入URL或持久浏览器存储。
6. 检索原段落、真实命中Child、分数来源、实际参数、候选计数与耗时诊断。
7. 按当前Task attempt报告真实阶段/批次进度，失败不显示成功。
8. 元数据字段发现、只读内建字段、同库有界批量赋值。
9. PostgreSQL词法派生索引和显式hybrid召回，保留semantic默认值。
10. 分库/全局候选预算和分数域排序，原生阈值与最终融合分分离。

不纳入 URL/Notion 同步、外部知识库、Q&A、OCR/多模态、Pipeline、独立Child编辑
或新任务系统。混合召回/跨库排序必须通过真实质量评测，不因确定性接口测试通过
就宣称检索质量改善。

T0–T14 分为前置与契约、内容保护、检索核心、前端工作区、验证与交付五阶段，
前后端和Schema整体放行。新增Schema仍走显式空库安装契约；已有数据库是否可
重建须另行确认，M9 reset授权不延伸到M10，Runtime不补列/改marker。
若必须保留旧库而无受支持升级路径，部署保持阻塞，不自行实施迁移或重置。

验收（计划）：人工增改删和禁用内容在重嵌入后保持；完整正文/短引用一致；
claim/版本/权限竞态关闭；预览、深链接、过期详情、进度、批量元数据和只读界面
全链路覆盖。真实PostgreSQL/MinIO、mock/replay浏览器和真实模型分别报告。
至少60条脱敏标注问题及1万检索单元验证召回、排序、性能与费用，达到方案约定
门槛；全部十项、质量门、部署确认和文档同步完成后才标记M10完成。

## 5. 实施顺序

```text
M0 -> M1 -> M2
            |
            v
           M3 -> M4 -> M5 -> M6 -> M7 -> M8 -> M9 -> M10
```

不并行开发跨里程碑业务逻辑。每个里程碑先合并数据契约和测试，再进入下一个里程碑。
M8 内部四批按治理 → 分块 → 检索 → 扩展顺序交付，第三批父子分块依赖第二批的
切分器改造；第四批各项相互独立、按需启动。M9 按“准备 → 原子切换 → 验收”
三个内部阶段推进，新增包契约/客户端先于注册表消费，旧表/接口最后退役；
DeepSeek/OpenAI 专项与语言模型 UI 同步精简为 `deepseek`、`openai` 和
`openai_responses`（OpenAI 双协议共享原生实现），前后端、空库安装/
reset、replay 与新 Run 恢复契约通过 M9 放行门后整体合并，不把中间阶段独立交付。
M10 在 M9 验收后启动，内部可按详细计划并行准备，但内容发布、模型正文、
词法索引、排名来源和前端严格契约必须联调，最终通过真实质量及Schema交付确认门。

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

M8、M9、M10 为 MVP 之后的增强里程碑，以各自小节内的验收条目为完成标准
（M8、M9 已完成，未启动项在小节内明确记录；M10 计划中）。任务拆分见
[M9 执行计划](../superpowers/plans/2026-08-30-rag-knowledge-m9-model-registry.md)与
[M10 执行计划](../superpowers/plans/2026-08-30-rag-knowledge-m10-quality-workbench.md)。
