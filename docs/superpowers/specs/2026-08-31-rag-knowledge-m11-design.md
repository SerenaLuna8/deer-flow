# RAG Knowledge M11：查询向量缓存、分段摘要索引与知识库系统设置设计方案

> 状态：方案稿，设计已评审通过、未实施（2026-08-31）。
> 前置里程碑：M0–M10 已完成并合入 main；本文基线 commit `b94d8b34`
>（工作树另有与知识域无关的未提交改动，不属于 M11 范围，实施时不得触碰）。
> 本文定义目标契约，不表示当前代码已具备这些能力，也不授权执行数据库重置。
> 执行拆分见 M11 执行计划（`../plans/2026-08-31-rag-knowledge-m11.md`，随本规格批准后生成）。

## 1. 目标与证据边界

M11 落地三项增量：一，查询向量的进程内缓存，消除 Agent 反复检索时对同一查询文本的重复
Embedding 开销；二，分段摘要索引，为"问题式查询与长事实段落表述有鸿沟"的召回缺口补一条
同向量空间的语义召回来源；三，把根 `config.yaml` 的 `knowledge` 块迁入 PostgreSQL，
在平台管理员系统设置中提供"知识库配置"管理面。

已确认的实施基线：

- M10 交付的混合检索、三分支终排、词法派生索引、任务进度、reparse/rebuild 分离契约全部保留，
  M11 不重新设计它们。
- 摘要模型来源采用系统级设置 + 库级开关（评审决策一）；摘要经独立后台任务生成（决策二）；
  缓存为进程内 LRU+TTL（决策三）；质量门扩展 M10 评测集（决策四）；配置变更重启生效（决策五）。
- Dify 1.17.0 的 DocumentSegmentSummary（摘要命中回卷原始分段取最高分）是行为参照，
  不复制其代码；其"摘要单独状态机 + 独立启停"不迁入，本项目以"行存在即可用"为契约。

当前代码依据：
[检索服务](../../../backend/packages/knowledge/actweave_knowledge/retrieval/service.py)、
[包契约](../../../backend/packages/knowledge/actweave_knowledge/contracts.py)、
[摄取发布](../../../backend/packages/knowledge/actweave_knowledge/ingestion/pipeline.py)、
[重嵌入](../../../backend/packages/knowledge/actweave_knowledge/ingestion/reembed.py)、
[任务 Worker](../../../backend/packages/knowledge/actweave_knowledge/tasks/worker.py)、
[宿主配置读取](../../../backend/app/knowledge/config.py)、
[宿主 ModelPort](../../../backend/app/knowledge/model_port.py)、
[vision_bridge 先例](../../../backend/app/system_runtime_settings/models.py)。

## 2. 范围

| 编号 | 本期能力 | 完成标准摘要 |
| --- | --- | --- |
| F01 | 查询向量缓存 | 同模型同查询文本命中缓存不再调 Provider；正确性零影响；诊断可见命中计数 |
| F02 | 分段摘要索引 | opt-in 库的段摘要参与语义召回并回卷父段；生成/失效/重建全生命周期确定；质量门证明问题式查询召回提升 |
| F03 | 知识库系统设置 | `knowledge` 配置全量入库，`system_admin` 在系统设置页管理；YAML 块墓碑化；保存强制探测；重启生效 |

不在 M11：摘要人工编辑、摘要词法索引、每库独立摘要模型、文档级/多段聚合摘要、prompt 可配置、
摘要独立检索模式、缓存持久化/跨进程共享、配置热重载、ANN 索引、Q&A 索引、多模态。
原有功能不重复建设。

## 3. 模块与不变条件

- `actweave_knowledge` 继续拥有业务实现并保持宿主无关；摘要的 LLM 调用通过 `KnowledgeModelPort`
  新增的端口方法进入宿主，包内不新写任何 chat 协议客户端。
- PostgreSQL 仍是唯一权威：摘要行、系统设置行都是 Schema V1 成员，ORM、`full_schema.sql`、
  catalog digest、中文注释、schema 测试五件套同批变更；MinIO 职责不变（只存原文件字节）。
- Provider 调用（LLM 摘要、Embedding、Rerank）不占数据库事务；每个真实批次及重试前回验
  authority/claim/lease，发布事务内再复验版本与绑定。缓存命中不产生 Provider 调用，
  也不豁免召回事务内的既有回验。
- 响亮失败原则不变：摘要任务失败不影响文档 ready；检索、发布、设置保存的一切冲突走既有
  `KNOWLEDGE_CONFLICT` / `KNOWLEDGE_MODEL_UNAVAILABLE` / 422 契约，不静默降级。
- 密钥纪律不变：MinIO secret key 走共享 secret-envelope 加密，永不出现在响应、日志、审计、
  浏览器；摘要 prompt 与召回材料不升级为系统指令。
- CONTEXT.md 新增术语 **Knowledge Segment Summary**（系统生成的召回辅助派生物，绑定单个
  Knowledge Segment，携带库向量空间中的 embedding，永不作为引用正文）；知识摘要模型在词汇上
  是 **System Model**（文本模型），不是 Provider Model 检索模型。

## 4. F01：查询向量缓存

### 4.1 结构与键

包内新增 `retrieval/query_cache.py`，`KnowledgeModule` 组合时构造一个实例，Gateway 检索测试
与 Worker Agent 工具在同进程内共享；Gateway 与 Worker 进程各自持有独立缓存。

- 键：`(embedding_model_id, SHA-256(查询原文 UTF-8 字节))`；在查询长度校验（≤2000 字符）
  通过后取键。值：查询向量（只读元组）。
- 只缓存查询向量。文档、子块、摘要的内容 Embedding 一律不缓存（唯一文本无复用价值）。
- LRU + TTL，读时惰性过期，写时容量淘汰。不做并发去重（single-flight）：并发未命中各自调
  Provider、后写覆盖，同模型同文本值相同，无正确性影响。
- 配置三项（来源见 F03 设置行）：`query_cache_enabled`（默认 true）、
  `query_cache_max_entries`（默认 512，边界 16..65536）、`query_cache_ttl_seconds`
  （默认 300，边界 5..86400）。`enabled=false` 时缓存对象仍构造但恒未命中，代码路径单一。

### 4.2 正确性与授权契约

- 缓存是正确性中立的：同模型同文本产生同一向量，命中与未命中的检索结果集必须逐字节一致；
  模型换绑（rebuild）后 `embedding_model_id` 变化天然隔离旧条目，无需显式失效逻辑。
- 授权措辞更新：既有"每次分组查询嵌入前回验授权"改为"每次**面向 Provider 的**查询嵌入
  （即缓存未命中）前回验"；命中路径没有 Provider 开销可截断，召回事务内与终审的回验照旧执行，
  撤权后的分段披露仍在既有边界被拒绝。`backend/AGENTS.md` 知识节同批更新此句。
- 缓存内容不进日志、不进诊断响应；`debug=true` 诊断新增 `query_embedding_cache_hits` 与
  `query_embedding_cache_misses` 两个本次搜索的计数字段。

### 4.3 验收要点

命中/未命中结果一致性（replay Provider 下字节级比对）、TTL 过期、容量淘汰、模型隔离、
禁用即恒未命中、撤权成员在缓存全热时仍被既有边界拒绝、并发同查询双搜索无死锁、
诊断计数正确。集成断言：第二次相同搜索的 Embedding Provider 调用次数为零。

## 5. F02：分段摘要索引

### 5.1 数据模型

新表 `knowledge_segment_summaries`：

| 列 | 约束 |
| --- | --- |
| id | UUID PK |
| project_id / knowledge_base_id / knowledge_document_id | UUID NOT NULL，召回作用域索引 |
| knowledge_segment_id | UUID NOT NULL，FK → knowledge_segments ON DELETE CASCADE，唯一约束（每段至多一条摘要） |
| document_version | INT NOT NULL，生成时文档执行代次（审计与迟到防护证据） |
| content | TEXT NOT NULL，摘要文本 |
| source_content_digest | TEXT NOT NULL，生成时源段落内容 SHA-256 |
| embedding | Vector NOT NULL，库当前 Embedding 模型空间 |
| created_at | timestamptz NOT NULL DEFAULT now() |

- 摘要行只以完整形态存在（文本 + 向量一次事务写入），没有独立 status/enabled；
  启停与可见性完全跟随所属 Segment 与 Document 的既有开关。
- 摘要不建词法字段：词法路只作用于真实内容。
- `knowledge_bases` 新增 `summary_index_enabled BOOLEAN NOT NULL DEFAULT false`。
- `knowledge_tasks` 新增种类 `summarize_document`，stage 枚举新增 `summarizing`
  （该任务 stage 走 `queued → summarizing → embedding → publishing → done`），
  纳入"同文档单开放任务"部分唯一约束族——因此与 ingest/reembed/reparse 在同一文档上
  互斥排队，开放的 summarize 任务会阻止 reparse/rebuild 准入（沿既有拒绝语义）；
  失败派生、过期恢复、Project purge 与 retry 继承规则同步识别新种类。

### 5.2 模型接入

- `KnowledgeModelPort` 协议新增摘要生成方法（输入段落文本，返回摘要文本；宿主内部自带
  超时与请求上限）。宿主 `app/knowledge/model_port.py` 用 harness `ModelRuntime` 实现，
  五种适配器（anthropic/deepseek/openai/openai_responses/vllm）天然可用。
- 摘要模型由 F03 设置行的 `summary_model_name` 指定（可空 = 系统级未启用）。保存时校验
  指向活跃文本 System Model；任务执行时每次认领重新解析当前值——**摘要模型变更即时作用于
  后续任务，不需要重启**（与存储/开关等重启生效字段在 UI 上分组说明）。
- 模型缺失、停用或解析失败：任务以既有类型化 `KNOWLEDGE_MODEL_UNAVAILABLE` 失败，
  文档保持 ready、摘要缺席，检索自然退化为无摘要现状。

### 5.3 生成流程

Prompt 固定于包内（v1）：要求以源段落语言输出不超过 200 字、保留关键实体与结论的摘要；
请求侧限制 `max_tokens ≤ 1024`，返回文本超过 1000 字符时硬截断。仅为字符数 ≥ 200 的段
生成摘要（短段的内容向量已足够，计入 skipped），空文档零段直接成功。

触发点（均要求库 `summary_index_enabled=true` 且系统级摘要模型已配置）：

1. `ingest_document` / 带 reparse 参数的 ingest 发布成功 → 同事务入队 `summarize_document`。
2. 库开关拨 ON → 在 Base 锁下按 UUID 序遍历全部已发布（`published_version` 非空且 ready）
   文档批量入队回填，响应报 accepted/skipped 计数（沿 rebuild 风格）；存在开放任务、
   非 ready 或未初始化的文档计入 skipped，不违反单开放任务约束。拨 OFF 只做召回排除
   （SQL 过滤开关位），摘要行保留，重开即刻生效，不重新生成未失效的行。
3. 段落 edit/add（同步治理路径）→ 同一写事务删除受影响段的摘要行，并入队该文档的刷新任务。

任务执行（`summarize_document` handler）：

1. 认领与租约沿既有 `knowledge_tasks` 语义；同事务校验 Project active、文档 ready 且
   version 匹配、库开关仍开，任一不满足按既有规则退回或失败。
2. 装载缺摘要的目标段（回填/刷新同一逻辑：现存 `source_content_digest` 与当前段内容一致的
   行跳过，不一致视为缺失重生成）。
3. 逐段调用 LLM 生成摘要，再按库 Embedding 模型 `max_batch` 分批嵌入；**每个 LLM 批次与
   Embedding 批次前回验租约与 Project 状态**，进度按成功批次累加 `completed_units`
   （单位 = 已完成摘要条数），attempt 重试从零计数。
4. 单事务发布：复验 claim、文档 version、库开关、Embedding 模型绑定；删除目标段旧摘要行、
   插入完整新行；全有或全无。版本不匹配静默放弃（迟到结果永不发布），任务按既有规则处理。
5. 失败保留文档 ready 与既有摘要行不变；attempts 耗尽后任务 failed，文档列表的
   task_progress 投影可见，用户可显式重试。

与重处理的交互：

- **rebuild（重嵌入）保留摘要文本，只重算摘要向量**：`reembed_document` handler 的发布事务
  同时用新模型重嵌入全部摘要 content（零 LLM 费用），与段/子块向量同一版本检查内原子切换。
- **reparse 级联覆盖**：段行替换触发 CASCADE 删除摘要，发布成功后（开关开启时）重新入队生成。
- 文档/段删除、库删除、Project purge 沿 FK 级联与既有清理路径，无需新增清理任务种类。

### 5.4 召回集成

- 摘要是 **semantic 路的第三个候选来源**，仅对 `summary_index_enabled=true` 的库生效：
  对 `knowledge_segment_summaries.embedding` 做精确余弦，命中**回卷到所属 Segment**；
  该段语义原生分 = max(自身段分 [general] / 子块最高分 [parent_child] / 摘要分)。
  同一向量空间内 max 回卷数学成立，分数域不变。
- 语义路合并三来源后再做每库预算 C 截断；召回 SQL 在 limit 前施加与既有两路完全一致的硬过滤
  （Project、Base、Document ready+enabled+version、Segment enabled、metadata 过滤）。
- RRF 每库合并、三分支终排、阈值只作用原生分、终审 stale 剔除等 M10 契约全部不变；
  **引用与 passage 永远是 Segment 真实内容**，摘要文本不进 citation、不进 ToolMessage 正文。
- Reranker 照旧对父段正文评分；词法路与 `lexical_version` 契约不受影响。
- 诊断：`debug=true` 计数新增 `summary_candidates`；`hit_diagnostics` 新增
  `matched_via=segment|child|summary`（取该段语义原生分的实际来源）。
- 段详情端点新增只读 `summary` 字段（存在时返回摘要文本与 created_at，UI 标注"系统生成摘要"）。

## 6. F03：知识库系统设置

### 6.1 设置行

新表 `knowledge_system_settings`（单行：`id SMALLINT PK CHECK (id = 1)`，乐观 `revision`）：

| 分组 | 列 | 约束/默认 |
| --- | --- | --- |
| 开关 | enabled | BOOL NOT NULL DEFAULT false |
| Worker | worker_concurrency / task_timeout_seconds | 1..16 默认 2；30..7200 默认 900 |
| 配额 | upload_max_bytes / max_knowledge_bases_per_project / max_documents_per_knowledge_base / max_segments_per_document | 与现 YAML 边界一致（上传硬上限 50 MiB；段上限 ≤5000） |
| 存储 | minio_endpoint / minio_bucket / minio_access_key / minio_secure | 前三者可空文本（endpoint 禁 URL scheme），secure BOOL DEFAULT false |
| 存储密钥 | minio secret（envelope 密文列） | 共享 secret-envelope 加密；可空 |
| 摘要 | summary_model_name | 可空文本；保存时校验活跃文本 System Model |
| 缓存 | query_cache_enabled / query_cache_max_entries / query_cache_ttl_seconds | 见 F01 边界 |
| 元数据 | revision / updated_at | 乐观锁；时间戳 |

CHECK：`enabled=true` 时 MinIO 四要素与密文列必须非空。全新安装 `make setup-db` /
`make reset-db` 播种 id=1 的禁用默认行。

### 6.2 管理 API 与审计

挂在既有 `system_admin` 管理路由族（与 admin model settings 同级）：

- `GET /api/admin/settings/knowledge`：全部非密字段 + `secret_key_configured` 布尔 + revision；
  密钥永不回显。
- `PUT /api/admin/settings/knowledge`：strict 模型 + 乐观 revision（冲突 409）；secret 字段
  可省略（保留旧值）；`enabled=true` 时**保存前强制**用提交的完整配置做存储探测
  （bucket 可达 + versioning 为 Off，短超时），探测失败 422、整体不落库；
  `summary_model_name` 非空时校验活跃文本 System Model，失败 422。
- 变更写审计（closed action `knowledge_settings.update`，actor/outcome 契约沿审计域规则，
  metadata 不含任何字段值与密钥）。

### 6.3 启动装配与降级

- Gateway / Worker lifespan：建引擎 → 读设置行 + envelope 解密 → 构建包契约 `KnowledgeSettings`
  （contracts 增缓存三字段；来源由 YAML 变 DB，包不感知差异）→ 组合模块 → 存储校验。
- **契约调整**：启动存储校验失败由"进程拒绝启动"改为"知识模块缺席 + readiness 报告 knowledge
  组件不健康（只报组件状态，不含端点/凭据）"。模块缺席时知识路由照旧 404
  `KNOWLEDGE_DISABLED`、Worker 不注册处理器、队列任务原地等待修复后重启；
  留存清理的 fail-closed 契约不变（存储配置缺失或损坏时 Project purge 失败并重试）。
- 除 `summary_model_name`（任务级即时生效，见 5.2）外，设置行全部字段重启生效；
  UI 与文档明确区分两种生效语义。

### 6.4 YAML 墓碑与迁移

- `config.yaml` 出现非空 `knowledge` 块 → 启动 fail-closed 拒绝，错误信息指向系统设置页与
  迁移脚本；`app/knowledge/config.py` 的 YAML 读取路径替换为墓碑检查 + DB 读取。
- 存量部署一次性显式迁移：`scripts/migrate_knowledge_config.py` 读取现 YAML 块（含环境变量
  展开），经 `KnowledgeSettings` 校验后加密写入设置行（幂等，重跑覆盖同值），随后操作者删除
  YAML 块再启动新版本。操作顺序：停服 → 迁移 → 删块 → 启动。
- `.env` 的 `ACT_WEAVE_KNOWLEDGE_MINIO_*` 此后只服务迁移脚本，正常启动不再消费。

### 6.5 前端

平台管理系统设置新增"知识库配置"页（仅 `system_admin` 可见）：功能开关、存储连接表单
（secret 写后不回显、以"已配置"占位）、配额上限、摘要模型下拉（数据来自既有管理端模型列表，
过滤活跃文本模型）、查询缓存参数；顶部常驻"除摘要模型外，保存后需重启 Gateway 与 Worker
生效"提示；保存失败按探测错误给出可操作文案（连不通 / versioning 非 Off / 权限不足）。

## 7. Schema、HTTP 与前端增量汇总

| 位置 | 变更 |
| --- | --- |
| 新表 knowledge_segment_summaries | 见 5.1 |
| 新表 knowledge_system_settings | 见 6.1 |
| knowledge_bases | summary_index_enabled |
| knowledge_tasks | summarize_document 种类、summarizing stage、约束族与恢复识别 |
| contracts.KnowledgeSettings | query_cache 三字段；来源改 DB |

| 接口 | 变更 |
| --- | --- |
| PATCH /bases/{id} | 接受 summary_index_enabled；拨 ON 入队回填并报 accepted/skipped |
| GET /model-options | 增 summary_model（已配置返回名称，未配置 null；前端据此禁用开关并提示） |
| GET .../segments/{segment} | 增只读 summary 字段 |
| POST /search | debug 增缓存计数、summary_candidates；hit_diagnostics 增 matched_via |
| GET/PUT /api/admin/settings/knowledge | 新增，见 6.2 |

前端：管理页新增知识库配置；库设置页新增摘要索引开关卡片（未配置系统摘要模型时禁用并提示）；
段详情/浏览器展示系统摘要；文档列表 task_progress 显示 summarize 任务与新 stage 文案；
i18n（en-US / zh-CN / types）同批补齐。`docs/knowledge/RAG知识库设计文档.md` 与
`backend/AGENTS.md` / `frontend/AGENTS.md` 知识相关段落同批更新。

## 8. 部署确认门

Schema V1 无迁移 ancestry：M11 的受支持安装验证路径是全新空数据库；存量数据库的处置是独立
操作者决策（停服、备份、确认目标后显式重建），**M10 的 reset 授权不延伸到 M11**。
配置迁移（6.4）针对 YAML → DB，与数据库重建是两件事，不得混同。本文只生成文档，
不执行任何 DDL、reset 或业务数据改动。

## 9. 验收与完成定义

### 9.1 确定性验收（replay Provider 全可复现）

- F01：见 4.3 全项。
- F02：生成→发布→召回回卷→matched_via 归因；段 edit/add 同事务失效 + 刷新；rebuild 保文本
  重算向量；reparse 级联删除 + 重建；迟到发布版本防护；开关拨 ON 回填计数、拨 OFF 召回排除
  且行保留、digest 一致跳过重生成；短段跳过；模型未配置/停用的类型化失败与文档 ready 不受损；
  引用与 ToolMessage 正文始终为 Segment 真实内容；预算 C、RRF、阈值、三分支与 M10 基线契约
  测试全部不回归。
- F03：CRUD 与 system_admin 权限、乐观 revision 冲突、探测失败不落库、secret 省略保留/写入
  不回显（响应/日志/审计逐路径断言）、YAML 墓碑拒绝、启动降级 readiness 证据、迁移脚本幂等、
  setup/reset 播种默认行、摘要模型即时生效与其余字段重启生效的边界。

### 9.2 真实质量与性能门

- 评测语料新增"问题式查询"类目：答案位于长段落且问句与正文表述存在明显鸿沟；开发集与独立
  验收集各 ≥10 题，标注与匹配规则沿 M10（完整父 Segment 为单位，三级相关性）。
- 放行阈值（摘要开启 vs 同语料摘要关闭基线）：该类目 Recall@candidate 与最终 Recall@10 均
  **提升 ≥5 个百分点**；全量类目 nDCG@10 回退 ≤0.02；无答案误召回率不高于基线；
  既有类目（精确标识符、自然语言、尾部答案）不低于 M10 放行水位。
- 性能：非 Provider P95 相对 M10 基线恶化 >20% 须优化或记录产品复审；缓存命中率经诊断计数
  报告（Agent 复用场景实测），摘要生成的单文档 LLM 调用次数与 skipped 计数入验收记录。
- 真实 Provider 预算或标注语料不可用时，按 M10 协议明确记录受阻项，不以 replay 分数或
  功能测试冒充质量结论；F02 未过质量门不得标记完成。

M11 三项功能、确定性门、真实质量门、Schema 交付确认与文档更新全部完成才标记完成。
