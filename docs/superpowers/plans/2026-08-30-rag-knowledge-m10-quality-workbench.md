# RAG Knowledge M10（检索质量与知识维护工作区）Implementation Plan

> 状态：已完成（2026-08-30 立项，2026-08-31 T0–T14 实施、真实质量门与部署确认完成）。
> 规范来源：[M10 设计方案](../specs/2026-08-30-rag-knowledge-m10-quality-workbench-design.md)。
> 前置：[M9 模型注册表计划](2026-08-30-rag-knowledge-m9-model-registry.md)完整验收。
> 立项稿只编写方案和计划；实施证据见各 Task 落地记录（交付门 T13/T14），
> 真实质量报告见 `docs/knowledge/m10-quality-eval-report.md`。

## 目标与非目标

交付 F01–F10：完整模型正文、安全重嵌入/重新解析、预览文件选择、文档定位、深链接、检索详情/诊断、真实进度、元数据增强、词法/混合召回、多库预算与排序。

保留 Knowledge Package、Gateway/Worker、M9 ModelPort、Project authority、现有轮询及 PostgreSQL/MinIO；不引入外部连接器、Q&A、OCR、多模态、Pipeline、插件平台、第二套任务系统或数据库自动升级。

## 实施纪律

1. Task 是内部工作项，不是各自可独立发布的版本。涉及 strict DTO/Schema 的变更在功能分支内联调后一起交付，不发布中间不兼容状态。
2. 每项按聚焦失败测试 → 最小实现 → 回归验证推进。仅为实现细节重复覆盖的测试可以整合，但权限、lease、版本和真实协议回归不得随重构删除。
3. 不改变 M9 的模型所有权、Provider 端点冻结、Secret recipient 和适配器。M9 的临时兼容问题必须在 Task 0 收敛，不能由 M10 扩建旧接口兼容层。
4. 现有数据库是否可重建须单独确认；M9 reset 授权不继承。本计划不引入迁移框架，Runtime 不补列/改 marker。临时测试库与操作者目标库必须区分。
5. 源码和接口门、真实 PostgreSQL/MinIO、mock/replay 浏览器、真实模型质量分别报告；外部403不是成功证据，也不通过 skip 绕过。

## 范围与任务对应

| 功能 | 主要任务 | 验收任务 |
| --- | --- | --- |
| F01 完整正文 | T1、T5 | T11、T13 |
| F02 安全重处理 | T1–T4、T12 | T13 |
| F03 预览选择 | T10 | T13 |
| F04 文档定位 | T9 | T13 |
| F05 深链接 | T5、T9、T11 | T13 |
| F06 详情/诊断 | T5、T7、T8、T11 | T13 |
| F07 阶段进度 | T1、T4、T12 | T13 |
| F08 元数据 | T1、T6、T12 | T13 |
| F09 混合召回 | T1、T7、T8 | T13、T14 |
| F10 多库预算/排序 | T7、T8、T11 | T13、T14 |

## 文件范围

现有文件（实施时重新核对，保持拥有者路径）：

```text
backend/packages/knowledge/actweave_knowledge/
  contracts.py __init__.py module.py
  bases/service.py documents/service.py segments/service.py metadata/service.py
  ingestion/pipeline.py ingestion/preview.py
  retrieval/service.py models/client.py
  persistence/models.py persistence/tasks.py persistence/derivations.py
  tasks/worker.py project_retention.py
backend/app/knowledge/gateway.py run_tool.py composition.py authority.py model_port.py
backend/packages/harness/deerflow/persistence/
  full_schema.sql final_schema_contract.py final_schema_digest.py schema_comments.sql
backend/scripts/generate_schema_comments.py setup_postgres.py check_postgres.py reset_postgres.py
backend/tests/knowledge/
backend/tests/replay_knowledge.py
frontend/src/components/projects/knowledge/
frontend/src/components/workspace/citations/knowledge-citations-panel.tsx
frontend/src/core/knowledge/ frontend/src/core/i18n/locales/
frontend/tests/unit/core/knowledge/ frontend/tests/unit/core/threads/knowledge-citations.test.ts
frontend/tests/e2e/project-knowledge.spec.ts
frontend/tests/e2e-real-backend/knowledge-real-backend.spec.ts
docs/knowledge/ README.md Install.md backend/AGENTS.md frontend/AGENTS.md
```

建议新增的实现文件（尚不存在，不先建立空框架）：

```text
backend/packages/knowledge/actweave_knowledge/ingestion/reembedding.py
backend/packages/knowledge/actweave_knowledge/retrieval/lexical.py
backend/packages/knowledge/actweave_knowledge/retrieval/ranking.py
frontend/src/core/knowledge/navigation.ts
frontend/src/core/knowledge/document-list.ts
frontend/src/components/projects/knowledge/knowledge-search-result-detail.tsx
backend/tests/knowledge/test_reembedding.py
backend/tests/knowledge/test_search_details.py
backend/tests/knowledge/test_lexical_retrieval.py
backend/tests/knowledge/test_search_ranking.py
backend/tests/knowledge/test_task_progress.py
backend/tests/knowledge/fixtures/m10_retrieval_cases.json
```

新增代码只在出现实际调用方时落地；不暴露内部 tokenizer、融合器、进度回调作为 Package 公共框架。

## 阶段 A：前置与契约

### T0：固定 M9 基线和交付前提

- [x] 记录 M9 验收 commit、工作树状态、实际前后端模型选项 DTO，以及完整验收证据。当前计划稿/旧行号不作为实现权威。
- [x] 验证 embedding_model_id、nullable reranker_model_id、原生分数与阈值语义、注册表引用保护、replay 引导，以及旧字段活跃消费者退役。
- [x] 将原生分数契约、未来融合分、操作类型、期望版本和 metadata field_kind 固定为测试样例；不得隐式选取另一模型。
- [x] 确认已有数据库交付约束。若不能重建且必须保数据，记录部署阻塞并另立审批事项，不自行实现 ALTER/reset 路径。
- [x] 收集第14项评测语料与真实 Provider 使用条件；没有标注数据或调用权限不妨碍设计/离线开发，但阻塞对应真实放行门。

验收：M9 不是靠名称标为完成；M10 的操作/分数/数据处置有明确记录，没有未经授权的破坏性动作。

T0 基线记录（2026-08-30）：

- M9 验收 commit `063b345b6325a5447314ba9d23ed91940ef54fcb`（`rag-knowledge`，
  T0 时工作树干净）。验收证据：后端聚焦 205 + knowledge/运行时 552 通过、
  `pnpm check` 干净、前端单测 1066 通过、三份 e2e（知识库/系统设置草稿/
  模型注册表）通过。
- 模型选项 DTO 实际形状：后端 `KnowledgeModelOptionsResponse
  {embedding_models, reranker_models}`（strict），前端 Zod 同构；
  包契约 `embedding_model_id` 必填、`reranker_model_id` 可空且改绑三态
  （`clear_reranker_model`）。阈值语义 `0=不过滤（负分通过）` 于
  `retrieval/service.py` 与既有测试确认；`model_configuration_id` 等旧字段
  在 src/tests 中零活跃引用；replay 引导 `seed_replay_model_registry` 就绪。
- 契约样例冻结于 `backend/tests/knowledge/fixtures/m10_contract_baseline.json`，
  由 `backend/tests/knowledge/test_m10_baseline_contract.py`（14 例）验算
  RRF 公式、候选预算、分数域、操作类型、field_kind、CAS 字段与交付约束。
- 数据库处置：已有数据库不授权重建（M9 reset 授权不继承），受支持安装
  路径为全新空库 Schema V1；测试仅用随机隔离临时库。操作者目标库的
  处置在 T14 前另行确认，未确认前部署阻塞。
- T14 真实质量门：标注语料与真实 Provider 预算均未提供，状态
  `blocked_pending_operator_input`；不阻塞 T1–T13 的设计与离线开发。

### T1：最终 DTO 与 Schema 一次定义

依赖 T0。

- [x] 在 contracts 中定义 SearchHit、score kind/domain、safe diagnostics、Segment detail（含旧内容只读状态）、reparse 输入/任务专用参数、task progress、filter field 和 batch metadata DTO；由 __init__/module 暴露最小业务 Interface。
- [x] 定义新增 Citation 字段的新写入规则和旧消息可缺省规则；hits 为结果唯一源，citations 派生，避免双重排序。
- [x] ORM/SQL 同步增加设计方案第10节字段与索引；Document.published_version 派生 content_initialized，避免双重状态；reembed 与 ingest 在同一 Document/target_version 上只能有一个开放索引操作，不能因 kind 不同绕过唯一保护。
- [x] 同步 catalog digest、注释源/快照、Schema 契约与 required relations；不增加新知识库模型表。
- [x] Gateway strict DTO、前端 strict Zod 与 route guard expected 清单同步准备。调试字段不得直接序列化 ORM、material 或异常对象。

测试：扩展 `test_schema_repository.py`、`test_package.py`、现有 Schema/注释/安装脚本测试；覆盖列类型、约束、索引、模型和快照一致性、未知字段拒绝、缺省历史 Citation。

验收：在隔离空库一次安装成功；不依赖 runtime 建表、旧字段双写或手工 stamp。

#### T1 落地记录（2026-08-30）

- 契约：`contracts.py` 新增检索模式、SearchHit/MatchedChild/诊断、Segment detail、reparse、task progress、filter field、批量 metadata 全部 DTO 与冻结常量；`__init__.py` 同步导出（`test_package.py` 契约测试覆盖）。`KnowledgeSearchResult.hits` 为唯一事实源，`citations` 为派生 property；`KnowledgeCitation` 三个新字段可缺省，旧消息反序列化不受影响。
- ORM/SQL：`knowledge_bases.retrieval_mode`、`knowledge_documents.published_version`、segments/children 的 `lexical_tsv`（GIN）+`lexical_version`、`knowledge_queries.top_score_kind/strategy_version`、`knowledge_tasks` 的 `reparse_settings/stage/completed_units/total_units/progress_updated_at`；开放索引唯一分部索引改名 `uq_knowledge_tasks_open_indexing`，同时覆盖 `ingest_document` 与 `reembed_document`。
- lexical 占位：两列 `NOT NULL` 携带 server default（空 tsvector、version 0）；version 0 语义 = 尚未按 lexical_v1 派生，词法路读侧对版本不一致明确失败（T8 实装 tokenizer 后写路径显式赋值）。
- 摘要：`full_schema.sql`/注释快照重生成（1352 列注释），catalog signature 仅 `columns` digest 变化，`SCHEMA_V1_CANONICAL_DIGEST=f0d76d50…2f496`；临时空库一次安装 + 签名复读通过。
- 写路径：摄取发布成功时写 `published_version=version`（`content_initialized` 由此派生）；检索服务组装 hits（cosine/rerank 两种 local_score_kind、score_domain 为模型 ID 域标签、content_digest=SHA-256），查询日志记录 `top_score_kind/strategy_version`。
- Gateway/前端投影边界：现有 HTTP 投影继续读派生 `citations`，round-trip 测试全绿；新字段的 HTTP/Zod 投影按各自任务落地（T5/T8/T9+），本任务只冻结包侧契约。
- 门禁：`tests/knowledge` 全量 514 passed（含 Postgres 一次性隔离库）；backend 其余 4523 passed；`make format`/ruff 干净。

## 阶段 B：证据完整性和内容保护

### T2：重嵌入当前内容

依赖 T1。

- [x] 扩展 Base rebuild 准入：持 Base/有序 Document 锁，拒绝上传/开放索引/删除冲突；content_initialized 区分已删空与未成功发布。
- [x] Base 改绑、Document.version++ 和 reembed Task 同事务；已初始化 ready/failed 入队，未初始化 failed 明确跳过，不擅自从原文件解析。
- [x] 新 handler 读取当前完整 Segment/Child，包括 disabled；general 嵌入父段，parent_child 只嵌入 Child；零条内容有效成功。
- [x] 发布仅更新向量/代次、published_version及状态，保留 UUID、文本、位置、enabled、source_position、metadata 和计数；失败保留原published_version，旧向量在非 ready 期间不能通过任何召回路使用。
- [x] retry、failure derivation、expired recovery、Worker 注册、Project retention 的开放任务扫描识别新 kind。手动 retry 继承失败语义。
- [x] 修正 Segment edit/add 的隐含保护：明确比较调用模型前的 Document.version、绑定和内容状态；保留 UUID 后迟到编辑仍必须冲突。

测试：新增 `test_reembedding.py`，扩展 `test_bases.py`、`test_governance.py`、`test_tasks.py`、`test_worker.py`。先证明当前实现丢失手工内容，再证明修复；覆盖两种模式、删空、禁用、失败重试、同维度异空间、失租/删除、迟到编辑。

验收：重嵌入前后内容/身份/启停完全一致，只有目标向量和处理代次变化；不调用 extractor 或 MinIO download。

#### T2 落地记录（2026-08-30）

- 准入（`bases/service.py::rebuild_knowledge_base`）：同一事务内锁 Base 后按 UUID 序锁全部 Document；任一文档处于 `uploading/queued/processing/deleting` 或存在开放索引任务（`ingest_document`/`reembed_document`，含 `retry_wait`）即整体拒绝。已初始化文档（`published_version IS NOT NULL`，含已删空与 failed-but-published）改绑 + `version++` + 入队 `reembed_document` 同事务完成；从未发布的 failed 文档明确跳过并保持 failed，返回 `KnowledgeRebuildResult{base, accepted_document_count, skipped_document_ids}`。
- Handler（`ingestion/reembed.py::KnowledgeReembedHandler`）：构造上无 object store/extractor 依赖，结构性不可能重解析。读取 `published_version` 代次全部行（含 disabled），general 嵌入父段、parent_child 只嵌入 Child；零行文档直接有效发布。发布事务复核 claim token、目标版本与当前模型绑定，原位更新向量并把父/子行代次统一翻转到新版本，保留 UUID/文本/位置/enabled/source_position/metadata/hit_count/计数；任何不匹配都按迟到结果 no-op 收敛，绝不落错误空间向量。失败保留原 `published_version` 与旧行，文档非 ready 期间召回路按 `status == "ready"` 过滤排除。
- 生命周期识别新 kind：`persistence/tasks.py` 定义 `INDEXING_TASK_KINDS`，failure derivation 与 expired recovery 对两种索引 kind 走同一失败派生（reembed 失败不清计数）；`retry_document` 继承最近一次索引任务的 kind（reembed 重试保留行与计数）；Worker 注册表挂载 `reembed_document`；Project purge 本就按 project 全量删任务行，kind 无关。开放索引唯一分部索引 `uq_knowledge_tasks_open_indexing` 数据库层拒绝同文档同版本跨 kind 并存（`test_schema_repository.py` 直接验证）。
- 迟到编辑保护（`segments/service.py`）：`update_segment`/`create_segment` 在调用模型前快照 `document.version` 与绑定模型，写事务内复核两者，任一变化即 `KNOWLEDGE_CONFLICT`——同模型重嵌入后保留同 UUID 的旧编辑同样冲突（`test_late_segment_edit_conflicts_instead_of_writing_stale_vectors` 用同模型 rebuild 专门验证版本比较）。
- Gateway/前端：`POST /bases/{id}/rebuild` 返回 `KnowledgeBaseRebuildResponse{item, accepted_document_count, skipped_document_ids, request_id}`；前端 `knowledgeBaseRebuildResponseSchema`（strict Zod）与 API 层同步，mock e2e 的 rebuild 响应补齐新字段。UI 汇总展示按计划归 T12。
- 门禁：`tests/knowledge` 全量 525 passed（含新增 `test_reembedding.py` 10 例：准入接受/拒绝、general/parent_child 身份保持、删空、失败保留、失租迟到、删除竞争 no-op、手动 retry 继承、迟到编辑冲突）；后端全量回归通过；`make format`/ruff 干净；前端 `tsc --noEmit` 干净。

### T3：显式原文件重新解析

依赖 T2。

- [x] 增加 reparse-preview：服务器按 Document 权威下载原文件，共用现有 extract/clean/split；权限复核与临时文件清理完整。
- [x] 增加 reparse 准入：expected_version、完整参数校验、ready/failed 和开放任务冲突；禁止借该接口换模型。
- [x] 确认后将新参数固化到 Task 专用 reparse_settings，version++、入 ingest；retry 继承此次任务参数。Document 参数只在成功发布时与 Segment/Child 同事务替换，失败保持旧内容模式，不让新参数解释旧行。
- [x] 文档维护只读投影可明确展示失败重处理后残留的“旧内容”；正常搜索仍只读当前 ready 版本，不恢复旧索引。

测试：扩展 `test_ingestion.py`、`test_governance.py`、`test_upload.py`、`test_authority.py`；参数预览/实际发布一致、CAS、下载后撤权、临时文件清理、人工文本覆盖仅发生在此操作、失败不丢旧行；general↔parent_child 重新解析失败后重嵌入，仍按已发布模式生成向量。

验收：两个按钮对应不同数据来源和任务语义，不能只靠不同文案区分。

#### T3 落地记录（2026-08-30）

- 预览（`documents/service.py::preview_reparse`）：按 Document 权威的 `storage_key` 服务器侧下载原文件到任务级临时目录（成功/失败/取消都清理），复用 `preview_document_chunks`（同一 `extract_clean_split`），参数先全量校验；下载/解析在 PostgreSQL 之外运行，完成后新事务复核 authority 并重读版本，CAS 不符或撤权则计算结果不出门（`test_authority.py` 用下载中撤权验证）。
- 准入（`documents/service.py::reparse_document`）：单事务锁行；仅 `ready/failed`、base `active`、`expected_version` CAS、无开放索引任务（`ingest/reembed × queued/running/retry_wait`）。通过后 `version++`、`status=queued`，参数固化到任务 `reparse_settings`（JSONB，8 项全量），文档参数列与计数一律不动。DTO 无模型字段，HTTP strict body 多余字段直接 422（禁止借道换模型）。
- Handler（`ingestion/pipeline.py`）：`KnowledgeTaskClaim` 携带 `reparse_settings`，`_begin_processing` 优先应用冻结参数（`from_reparse` 标记）；发布事务在替换 Segment/Child 行的同时把 8 个参数列写回文档并更新 `published_version`。失败路径不碰参数列——新参数永不解释旧行。`reparse_settings` 列绑定 `none_as_null`，显式 None 落 SQL NULL 而非 JSON `'null'`（CHECK 约束验证）。
- retry 继承（`retry_document`）：继承最近一次索引任务的 kind 与 `reparse_settings`；计数清零条件改为 `published_version IS NULL`（从未发布），失败的 reparse/reembed 保留计数描述残留旧行。
- 旧内容投影（`list_document_segments`）：改按 `published_version`（无则按 version）过滤——重处理失败后维护页继续显示残留旧行（`document_version` 可见），正常检索仍只经 `status='ready'` 文档，不恢复旧索引。
- Gateway：`POST /documents/{id}/reparse-preview`、`POST /documents/{id}/reparse`（均 edit 能力，guard 清单测试同步）；预览响应含 `document_version+items+total`，reparse 返回标准 document mutation 响应。前端 UI 与 Zod 投影按计划归 T10/T12。
- 门禁：`tests/knowledge` 全量 532 passed（新增 7 例：预览/发布一致与参数固化、准入矩阵、失败保留与投影、retry 继承、HTTP round-trip、下载后撤权清理、模式切换失败后按已发布模式重嵌入）；`make format` 干净。

### T4：真实进度和尝试状态

依赖 T2/T3。

- [x] Task stage/计数更新验证 claim、target_version 与当前 attempt；领取新 attempt 清零，不累计旧尝试。
- [x] embed/rerank 每个真实HTTP批次及重试前执行相应authority/lease guard，embedding成功批次另更新进度；集中在现有客户端/handler协作处，不重复外部重试，不持事务等待模型。
- [x] Document 列表/详情返回绑定当前代次的 task_progress；queued/retry_wait/failed/done 正交，失败保留失败阶段。
- [x] 部分批次成功不能提前 ready；最终 publishing/done 和 Task success 仍与正式内容/向量发布同事务。

测试：新增 `test_task_progress.py`，扩展 `test_models.py`、`test_worker.py`；第二批失败/撤权、429重试前撤权、取消、失租、旧进度晚到、重试归零、Project pending deletion 不消耗尝试、不泄露执行材料；rerank同样停止未派发批次。

验收：能够解释当前在做什么；没有可测总数时不显示百分比，失败不显示嵌入完成。

#### T4 落地记录（2026-08-30）

- 客户端批次钩子（`models/client.py`）：`embed`/`rerank` 新增 `batch_guard`（每次真实派发前运行，含客户端内部对 429/5xx/传输错误的单次重试前；抛错即停止未派发批次）与 `on_batch_verified`（embed 专用，响应校验通过后按批大小回调）。`_post_with_retry` 的两次尝试各自先跑 guard。探针与单文本路径不传钩子，行为不变。
- 进度持久化（`persistence/tasks.py`）：`claim_next_task` 领取时归零（`stage='queued'`、`completed_units=0`、`total_units=NULL`、刷新 `progress_updated_at`）——新 attempt 不累计旧尝试。新增 `update_task_progress`，UPDATE 同时匹配 claim token、`status='running'`、`attempt_count`、`target_version`，旧 attempt 晚到的进度 rowcount=0 不落地。两条 settle 成功路径（发布事务内 `settle_task_row_success` 与 worker 的 `settle_task_success`）统一盖 `stage='done'`——done 只随成功结算出现；失败结算不碰 stage（失败保留失败阶段，重试等待期计数可见）。
- Reporter（新 `ingestion/progress.py`）：`KnowledgeTaskProgressReporter` 持有 attempt 的 stage/计数，全部经独立短事务写入（不持事务等待模型）；`ensure_claim_alive` 为只读 guard。写入不匹配或 guard 失败抛 `KNOWLEDGE_TASK_FAILED`，失效 claim 的结算本身也是 token-guard 的 no-op。
- Handler 接线：ingest（`pipeline.py`）reading_source → extracting_splitting → embedding（total=向量条目数：general=段数、parent_child=子块数）→ publishing → done；no-op claim（版本不匹配等）不报任何 stage。reembed（`reembed.py`）loading_segments（begin 前，兼首次 claim 验证）→ embedding（total=len(entries)，0 行直接 publishing）→ done。两个 handler 的 embed 均挂 guard+verified 钩子。
- 检索路径（`retrieval/service.py`）：embed/rerank 的"调用前一次 revalidate"下沉为 `batch_guard`——每个真实批次与重试前复核 authority，rerank 批间撤权停止未派发批次；revalidation 次数对单批场景与原实现一致（既有 5 次断言不变）。
- 投影（`documents/service.py`）：`indexing_task_progress` 取绑定当前代次（`target_version == document.version`）、`status != succeeded` 的最新任务行投影为 `KnowledgeTaskProgress`（`next_attempt_at` 仅 retry_wait 时为 `available_at`）；succeeded 与旧代次不投影，DTO 字段集固定，不含 claim/lease/storage 材料。接入 list/detail（`_load_document` 顺带返回）。
- HTTP/Zod：Gateway `KnowledgeTaskProgressResponse`（strict）挂到 `KnowledgeDocumentItemResponse.task_progress`；前端 `knowledgeTaskProgressSchema` + document item schema 同步（strict、必填 nullable），e2e mock `documentView` 补 `task_progress: null`。UI 渲染按计划归 T12。
- 测试：新增 `test_task_progress.py` 7 例（真实 `KnowledgeModelClient` + async MockTransport 中途查库：stage 流转与逐批计数、第二批失败保留已验证进度且不提前 ready、重试归零后走完、失租停止未派发批次且晚到进度不落地、429 重试前 guard 阻止第二次请求、reembed 流转、文档投影含字段集与泄露检查）；`test_models.py` +4（guard 时序/重试前 guard/rerank guard 停批）；`test_tasks.py` +3（claim 归零、`update_task_progress` 的 stale/attempt/version 拒绝、`settle_task_row_success` 盖 done）——计划中写给 `test_worker.py` 的 claim 级用例落在 `test_tasks.py`（persistence 原语所在测试文件）；`test_upload.py` +1（HTTP 投影 round-trip）。Project pending deletion 不消耗尝试由既有 defer 测试覆盖（T4 未改该路径）。
- 门禁：`tests/knowledge` 546 passed；前端 `pnpm check` 干净、knowledge 单测与 `project-knowledge.spec.ts` e2e 29 passed；后端全量回归绿（含 `make format`）。

### T5：完整 passage、详情与引用投影

依赖 T1，可与 T2–T4 的内部实现并行，集成后一起验收。

- [x] 搜索保留完整候选快照、document_version、content_digest 和真实 Child 命中；最终统一复核内容、权限及当前 metadata/builtin 硬过滤后生成 SearchHit。
- [x] Agent 用完整 passage 构造正文，UTF-8 JSON 上限64KiB，整段选择；additional_kwargs 只带实际发送项的短引用，给 omitted_count，不新增第二套 LLM 计数器。
- [x] 更新工具说明，不把排名分数说成正确性概率；保留现有错误 ToolMessage 无引用规则及宿主 Provider capacity guard。
- [x] 新增单 Segment 详情和 Child 分页，验证资源关系和期望版本/digest；普通浏览与旧检索详情过期语义明确区分。
- [x] Query/hit count 仍是检索结果统计，不谎称全部已发送给模型；工具单独报告发送数量。
- [x] HTTP 普通结果不泄露 passage；debug 按最终引用投影逐hit安全诊断、真实matched_children和局部分数，不返回Child/未入选候选正文。历史 Citation 缺新字段依然可显示短引用，不反推旧来源。

测试：新增 `test_search_details.py`，扩展 `test_retrieval.py`、`test_agent_tool.py` 和前端 citation 单测。覆盖答案在320字符后、4000字符中英文段、全包字节预算、结果/引用一致、同ID内容改动、重嵌入/重解析后旧详情、跨项目及失权；rerank等待期间批量赋值、字段改名/删除及文档改名不得漏过最终过滤。

验收：模型可读正文与界面摘要分离；测试/Agent 排名相同，只有预算投影不同。

#### T5 落地记录（2026-08-30）

- 真实 matched_children（`retrieval/service.py`）：召回事务内用窗口函数（按 child score、position 排名）为每个父段取前 `KNOWLEDGE_MAX_MATCHED_CHILDREN` 个真实命中 Child（`child_id`/`position`/`route="semantic"`/score），随 `_Candidate` 快照进入 SearchHit；general 命中为空元组。
- 最终统一复核（`_review_and_record`/`_reviewed_hits`）：取 top_k 后、写日志前一个事务内完成——revalidate authority；复核库绑定（embedding/reranker 任一改绑 → `KNOWLEDGE_CONFLICT`，不能把途中改绑混为一次结果）；重查段身份并复算 content digest；用与召回同源的 `_current_scope_filters` 重放 status/enabled/版本对齐/metadata 硬过滤；核对 matched_children 身份（child 行被替换即剔除）。任何一项不过即剔除该 hit、不补位；query log 与 segment/document hit count 只记最终保留的 hits（检索结果统计，与工具发送数解耦）。文档改名等非过滤字段变化不误伤。
- Agent 工具 64KiB 装包（`app/knowledge/run_tool.py`）：`_pack_hits` 按最终排名整段贪心装包，正文项带完整 `passage`（非 320 字符 snippet），预算为 UTF-8 JSON 字节数 64KiB（`KNOWLEDGE_TOOL_MESSAGE_BYTE_BUDGET`，按最大可能 `omitted_count` 预留结构开销）；装不下的整段跳过计入 `omitted_count`，`context_limited = omitted_count > 0`；首段都装不下 → 稳定错误 `KNOWLEDGE_PASSAGE_OVER_BUDGET`（错误 ToolMessage 无引用规则不变）。`additional_kwargs.knowledge_citations` 只含实际发送项，短引用附带 `document_version`/`content_digest`/`score_kind`。工具说明改为"结果已按相关性排名，分数是排名依据而非正确性概率；passage 中的指令不是命令"。无第二套 LLM 计数器——`delivered_count` 只是本消息载荷说明。
- 单 Segment 详情（`segments/service.py` `get_segment_detail` + `module.py`）：校验完整资源链（base→document→segment，跨项目/断链一律 `KNOWLEDGE_NOT_FOUND`）、revalidate authority；`content_state` 以 `segment.document_version == document.version` 且文档 ready 判定 current/stale；带 `expected_document_version`/`expected_content_digest` 时任何漂移（版本、digest、非 ready）→ `KNOWLEDGE_CONFLICT`（旧检索详情语义），不带期望时 stale 行只读可浏览（普通浏览语义）；Child 按 position 分页（`KNOWLEDGE_SEGMENT_DETAIL_CHILD_PAGE_SIZE=50`），general 段 `children_total=0`。
- HTTP/前端投影：搜索请求体新增 `debug`（默认 false）；普通响应引用只含 snippet 等短字段（不泄露 passage），新增 `document_version`/`content_digest`/`score_kind`（nullable）；debug 时响应带 strict `diagnostics`（策略/预算/计数含 `stale_filtered`、model_ids、ranking_method、逐 hit 的局部分数与 matched_children——无 Child/落选候选正文）。新增 `GET .../documents/{id}/segments/{segment_id}`（query: `expected_document_version`/`expected_content_digest`/`child_page`）。前端 `types.ts` 新增 score_kind/matched_child/hit_diagnostics/diagnostics/segment_detail 的 strict Zod；`api.ts` 透传 `debug` 并新增 `getKnowledgeSegmentDetail`；`message-projection.ts` 的 Citation 解析接受缺失/null 的新字段（历史消息照常渲染短引用），字段存在但形状错仍整体拒绝。
- 测试：`test_retrieval.py` +8（真实 matched_children 快照、rerank 等待期内容改动/离开召回范围（段禁用/文档禁用/重摄取）/metadata 批量赋值剔除且文档改名保留/中途改绑 embedding/reranker → 409/child 行替换剔除、debug 逐 hit 诊断只含最终 hits、HTTP debug round-trip 与普通结果不带诊断）；`test_agent_tool.py` +5（320 字符后答案完整送达、4000 字符中英文段、字节预算跳过与 omitted 计数、首段超预算稳定错误、工具说明分数措辞）并断言引用带新字段；新增 `test_search_details.py` 12 例（分页、资源链、期望通过/漂移冲突矩阵、stale 只读、失权、general 零 Child、HTTP round-trip 与默认参数/错误映射）；`test_upload.py` 路由能力守卫 +1；前端 citation 单测 +2（新字段有效/缺失/null 容忍、形状错误整体拒绝），e2e mock 对齐新契约。
- 门禁：`tests/knowledge` 573 passed；前端 `pnpm check` 干净、unit 全绿；`make format` 已跑。UI 消费（score_kind 展示、详情定位、debug 面板）按计划归 T11/T12。

## 阶段 C：元数据与检索核心

### T6：字段发现与批量赋值

依赖 T1。

- [x] 实现 builtin/custom 字段发现与类型/操作说明，内建值从 Document 权威动态投影，禁止伪造 uploader 历史。
- [x] metadata filter 增加 field_kind，默认 custom；未知 custom 字段仍按不匹配，AND/max10/类型保护不变；全部召回路复用同一个过滤构造。
- [x] 实现同库最多100文档、20字段的共同 patch，未传保留/null清空；全事务回滚，不用逐文档提交。
- [x] 统一字段增改删与单/批量赋值锁序，防止 rename/delete 后旧字段回流；元数据变更不触发 embedding。
- [x] 增加绑定 Project authority 的只读 knowledge_metadata_fields 工具，仅返回字段定义，不扫描值、不添加写能力；超过发现总量明确提示缩小库范围。

测试：扩展 `test_metadata.py`、`test_retrieval.py`、`test_agent_tool.py`、`test_authority.py`。覆盖内建同名冲突、禁止写 builtin、时间/number 类型、批量越权全回滚、并发字段改名、工具缺能力与旧字段定义后续失效。

验收：用户和 Agent 能发现允许过滤的字段；批量维护无需逐文档操作，也不会误清空未编辑字段。

#### T6 落地记录（2026-08-30）

- 字段发现（`metadata/service.py::list_filter_fields`）：每库返回 4 个内建字段（`document_name`/`uploaded_at`/`file_type`/`source_type`，`writable=false`）+ 全部 custom 字段定义（`writable=true`），操作符按类型来自 `KNOWLEDGE_FILTER_OPERATORS_BY_TYPE`（string: eq/contains，number/time: eq/gte/lte）。不带 `base_ids` 时扫描项目全部 active 库，超过 `KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES=20` 明确拒绝并提示用 `base_ids` 缩小范围（绝不静默截断）；显式 `base_ids` 去重后同样限 20，未知/深删中/跨项目库 → `KNOWLEDGE_NOT_FOUND`。custom 同名 builtin 不冲突——两者并列返回，靠 `kind` 区分。
- builtin 过滤（`retrieval/service.py`）：`KnowledgeMetadataFilter` 增加 `field_kind`（默认 custom，向后兼容）；builtin 走 Document 权威列动态投影——`document_name`→`name`、`uploaded_at`→`created_at`（epoch 秒）、`file_type`→`original_name` 扩展名小写、`source_type`→固定 `file_upload`，不写入 `doc_metadata`、不伪造 uploader 历史。builtin 未知字段/不支持的操作符/类型不符 → `KNOWLEDGE_INVALID_REQUEST`（custom 仍是"不匹配"语义）；AND/max10 不变。general 与 parent_child 两条召回路及 T5 最终复核（`_current_scope_filters`）复用同一个 `_metadata_filter_conditions` 构造——文档改名后复核期 `document_name` 过滤同样重放（专项测试验证）。
- 批量赋值（`metadata/service.py::set_documents_metadata`）：同库 ≤100 文档、≤20 字段的共同 patch；未传字段保留、null 清空；builtin 名与未定义字段名拒写。单一写事务内锁 Base → 按名取字段定义 → 按 UUID 序锁全部文档，任何一个文档缺失/跨库/深删中/值类型不符 → 整批回滚（`KNOWLEDGE_CONFLICT`/`KNOWLEDGE_INVALID_REQUEST`），不逐文档提交；成功按入参顺序返回视图。元数据变更不触发任务、不动 version/计数（专项测试断言无新任务行、版本不变）。
- 统一锁序：`create/rename/delete_metadata_field`、单文档 `set_document_metadata`、批量 `set_documents_metadata` 全部先 `FOR UPDATE` 锁 Base 行再锁字段/文档行——rename 与批量赋值并发时后者必然看到新字段名，旧键不可能回流（真实 PostgreSQL 并发测试验证）。
- Agent 工具（`app/knowledge/run_tool.py::create_knowledge_metadata_fields_tool`）：只读 `knowledge_metadata_fields`，闭包绑定宿主 Project authority（模型不可注入身份），可选 `knowledge_base_ids` 缩小范围；仅返回字段定义 JSON（kind/name/type/operators/writable + 使用说明），不扫描值、无写能力、无 citations payload；非法 UUID 不触库即报错，模块错误（含"缩小范围"提示、缺能力 `KNOWLEDGE_FORBIDDEN`）以标准错误 ToolMessage 透传。`knowledge_search` 的 `metadata_filters` 参数同步支持 `field_kind`。
- HTTP（`gateway.py`）：`GET /knowledge/filter-fields?base_ids=...`（read 能力）返回 strict 发现视图；`PATCH /knowledge/bases/{id}/documents/metadata`（edit 能力）批量赋值，返回按入参顺序的 document batch 视图；搜索请求体 `metadata_filters[].field_kind` 透传。路由能力守卫清单测试同步 +2。
- 测试：`test_metadata.py` +11（发现顺序/作用域/预算/失权、批量顺序与保留/清空、边界与 builtin 拒写、越权/跨库/坏值全回滚、不触发任务、并发 rename 无旧键回流、两个 HTTP round-trip 与错误映射）；`test_retrieval.py` +3（四个 builtin 过滤命中/未命中矩阵与 custom 同名并存、文档改名后最终复核重放 builtin 过滤、非法 field_kind/未知 builtin/坏操作符/坏类型参数化拒绝）；`test_agent_tool.py` +7（工具只读定义/重读/缩小范围/非法 UUID 不触库/错误透传/缺能力/说明措辞 + field_kind 透传默认 custom + lead agent 双工具挂载）；`test_authority.py` +2（filter-fields 读撤权、批量赋值写撤权整批回滚）。
- 门禁：`tests/knowledge` 全量 600 passed；`make format` 干净。批量 metadata UI 按计划归 T12。

### T7：分库预算、分数来源与诊断基础

依赖 T5/T6。

- [x] 按设计公式计算每库/每路 C 和全局400父段预算，保留同 Embedding query vector 复用；Child 先回卷去重，不先用原始子块占满父段预算。
- [x] 捕获每次搜索的有效模型/参数快照；模型调用前与结果返回前复核，不能将途中改变的绑定混为一次结果。
- [x] 原生局部分数/阈值与最终 ranking_score 分开；先保持单一比分域的 semantic 原生语义，准备融合分及 Query provenance 字段。
- [x] safe debug 记录实际计数/阶段耗时和空结果原因；模型/数据库失败保持错误而非空成功。检索正文不写诊断表或普通日志。
- [x] 取top_k后统一最终复核，stale剔除不补位；计数/日志/空原因与实际返回一致，不能为补齐top_k再次调用模型。

测试：新增 `test_search_ranking.py`，扩展 `test_retrieval.py`。覆盖大库+小库、同 E 不同 R、同 R 不同 E、NULL R、全局预算、负 cosine、阈值0/正值、共享名次与稳定破同分、交换输入顺序、stale剔除不补位、绑定变更、日志最高分及类型同源。

验收：候选预算不再由同组大库独占；原始分数不丢失，也不会被错误解释成融合分。

#### T7 落地记录（2026-08-30）

- 预算（`retrieval/service.py`）：新增 `calculate_per_base_budget(top_k, N) = min(B, floor(400/N))`（`B=calculate_candidate_k`，G 用 `KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET`）；`C<1`（目标库 >400）显式 `KNOWLEDGE_INVALID_REQUEST` 并提示用 `knowledge_base_ids` 缩小，不静默忽略库、不派发任何模型调用。两条召回 SQL 改为按 Base 分区的 `row_number()` 窗口（分数→文档→位置→UUID 稳定序）在 limit 前完成同一套硬过滤，SQL 内即封顶每库 C；general 与父子回卷两路在库内合并后再截 C（parent_child 先 GROUP BY 回卷取最佳子分，子块永远占不满父段预算）。同 Embedding 的 query vector 继续按模型缓存复用；全局 ≤ N×C ≤ 400 由公式保证。
- 快照复核：搜索开始即冻结 `snapshot_bindings`（全部目标库的 (E,R)）；`_revalidate_dispatch` 作为 batch_guard 在每个真实 Provider 批次与客户端内部重试前运行（authority + `_assert_snapshot_bindings`），批间改绑在下一批派发前即 409（真实 client + MockTransport 证明第二批未发出）；最终复核事务内同样复核全部目标库（不只有命中的库——无命中库改绑同样冲突），库被删除不算改绑（行自然离域被剔除）。`_PendingHit` 携带模型对的旧机制删除，pending 直接是 `KnowledgeSearchHit`。
- 排序分支（§8.3，词法归 T8）：分支由目标库绑定的策略决定而非幸存候选——单一分数域（全库同一非空 Reranker，或全无 Reranker 且同一 Embedding）保持原生排序与原生 `citation.score`；异构分数域走秩融合：域内先按库阈值过滤原生分，再 RANK 共享名次（1,1,3），`rank_score = 61/2 × 1/(60+domain_rank)`（词法项为 0，上限 0.5），融合分相同者只按 Base/Document/位置/Segment UUID 稳定破序、绝不制造分差；`citation.score`/`score_kind`、`ranking_method`/`ranking_score` 同行给融合值，`local_score`/`local_score_kind`/`score_domain` 保留原生证据。移除旧的每组 `[:top_k]` 预截断（融合域名次需要全量阈值幸存者）；`heterogeneous_without_lexical_evidence=true` 在异构且无词法证据时随诊断返回。
- safe debug：`counts` 填真实值（semantic_candidates=各组封顶后候选和、threshold_filtered、stale_filtered=pending−hits、parents_deduplicated=装配期真实去重、returned=len(hits)）；`timings` 用 `time.monotonic()` 分段累计 embed/recall/rerank/final review；`empty_reason` 四值按管道出口判定（无目标库→`not_ready`（含 debug 下的零目标诊断形状 `_empty_scope_diagnostics`）、召回为零→`no_candidates`、阈值全滤→`filtered_out`、复核全剔→`stale_candidates`），模型/数据库失败仍走错误契约；诊断仅存在于响应，正文/子块文本不进诊断与普通日志。`per_base_route_budget` 语义改为 C。Query 日志 `top_score`/`top_score_kind` 与最终返回同源（融合时记 `rank_fusion` 融合分）。
- 测试：新增 `test_search_ranking.py` 12 例（公式矩阵与 C<1、401 库先拒不派发、大库 25 段不再挤掉小库黑马（rerank 翻盘）、同 R 不同 E 原生排序+日志同源、同 E 不同 R 融合+embed 复用一次+同分身份序、NULL R 异构域+负 cosine 过零阈值+正阈值仍作用于原生分且分支不回退、RANK 共享名次 1,1,3 跨域稳定复跑、交换输入顺序结果与计数不变、无命中库中途改绑仍 409、批间改绑停止未派发批次、真实计数与 monotonic 耗时、四种 empty_reason 与成功时为 None）；`test_retrieval.py` 3 例异构组测试按融合语义更新（原始分数保留在 local_score、融合分 0.5/61÷2÷62、不再跨模型比较原始数值）。
- 门禁：`tests/knowledge` 全量 612 passed；`make format` 干净。HTTP/Zod 投影 T5 已就位（本任务无前端改动），score_kind 展示与诊断面板 UI 归 T11/T12。

### T8：词法索引、hybrid 与最终融合

依赖 T7，和 T2/T3 内容发布路径联调。

- [x] 实现包内 lexical_v1 纯规范化/词元生成，固定中文二元词元、英文/数字、完整业务标识符/IP 规则及方案约定的字节上限、长词元哈希；词元安全编码，原文保持原样。
- [x] 用 PostgreSQL simple tsvector + GIN、参数化 OR tsquery、固定 ts_rank_cd 规则实现词法路；不声称 BM25、不依赖数据库中文扩展或下载词典。
- [x] 正式入库、人工增改、Child 重切、reparse 同事务维护词法字段；parent_child 的父 Segment 仍生成派生字段用于统一排序，reembed 不改内容或词法。用真实 PostgreSQL 证明索引表达式/ORM快照一致。
- [x] Base 默认 semantic，显式 hybrid 才增加词法候选；检索测试可覆盖本次模式，不能将临时选择保存到 Base。
- [x] 两路按父段 RRF 合并；无 Reranker 的词法新增项仍计算 cosine 并应用原阈值，parent_child 取全部当前Child最大cosine，不能用NULL父向量或只用词法命中Child。
- [x] 按方案第8.3节接通统一 Reranker、同域原生、异域/无统一Reranker融合三分支；不新增模型选择配置、不按UUID隐式挑模型。
- [x] 有hybrid库且需融合时，所有入围父段统一计算词法排序证据，不偏向词法top C；全semantic不执行词法查询/128词元限制，不收紧原query长度契约。
- [x] 固定词元识别顺序、重复/位置规则及输入→词元→排名快照；使用RANK共享名次，原生阈值先于最终域内/词法排名，同分不由输入顺序改变。
- [x] 为纯语义异域无词法证据返回明确质量限制；策略常量只在评测后统一修订，不临时增加权重调参面板。

测试：新增 `test_lexical_retrieval.py` 和纯 tokenizer 测试；覆盖中文无空格、IP、接口名、错误码、大小写/全半角、标点注入、超长词元/查询、零词元、禁用/删除/metadata、同父多Child、词法索引随编辑更新及真实 query plan。

验收：功能性验证与 T14 质量验证均通过才完成 F09/F10；不是“SQL能返回结果”就宣布效果提升。

#### T8 落地记录（2026-08-30）

- Tokenizer（新增 `retrieval/lexical.py`，纯函数无 IO）：NFKC → lower；识别顺序固定为 完整 IPv4/IPv6（含无效 IP 回退普通切分）→ 业务标识符整体+部件（`error.code_v2` 产出整体与各部件）→ ASCII 词 → 连续汉字二元组（单汉字整体成词元）；重复词元保留、不记位置权重。`encode_lexical_token` 封闭字母表编码：词元 UTF-8 ≤128 字节存 `x`+hex（可逆），更长存 `h`+完整 SHA-256 hex（原文不动），`[xh0-9a-f]` 字母表使 tsquery 语法冲突不可构造。索引输入上限 256KiB，超出抛 `KNOWLEDGE_INVALID_REQUEST` 明确失败（绝不静默截断丢文本）；查询输入同规则派生、仅查询侧去重。快照测试锁 `KNOWLEDGE_LEXICAL_VERSION=1` 的输入→词元字面量，版本不升不许改行为。
- 词法路（`retrieval/service.py`）：`plainto/websearch` 不用——查询词元经 `encode_lexical_token` 后以参数化 OR `to_tsquery('simple', :q)` 执行，`ts_rank_cd(lexical_tsv, query, 2)`（按文档长度归一化）排名；GIN 索引表达式与 ORM 快照由真实 PostgreSQL 的 `test_schema_repository.py` 验证。general 直查父段，parent_child 查 Child 后 GROUP BY 回卷父段取最佳词法分，两路各按 C 封顶（窗口函数同语义路）。
- 写路径同事务维护：ingest 发布（含 reparse）、`create_segment`/`update_segment`、`_replace_children` 全部在内容写入同事务写 `lexical_tsv=to_tsvector('simple', lexical_index_input(content))` 与 `lexical_version=1`（父段与 Child 都派生，父段用于统一排序）；reembed 结构性只翻代次/向量，专项测试断言词法两列逐字节不变。
- hybrid 准入：Base `retrieval_mode` 默认 semantic；请求级 `retrieval_mode` 只覆盖本次调用（不落库，专项测试验证 Base 行不变）。目标含 hybrid 库且本次未强制 semantic 时才构建词法查询；查询去重后 >128 词元 → `KNOWLEDGE_INVALID_REQUEST` 并提示缩短或切语义（纯语义搜索不执行词法查询、不受 128 限制、原 query 长度契约不变）；零词元查询静默走纯向量路。hybrid 范围内发现 `lexical_version≠1` 的行 → `KNOWLEDGE_CONFLICT` 明确失败（绝不静默降级）——含融合分支下入围父段属于 semantic 库但未派生的情况。
- 两路合并与三分支（§8.3）：库内 semantic/lexical 两路按父段 RRF（k=60，`Σ 1/(60+rank)`，两路贡献直接相加）合并成候选池再截 C；词法新增项一律回填 cosine（parent_child 取当前全部 Child 的最大 cosine，NULL 向量不可能进入比较）并先过库阈值。最终排序仍按 T7 三分支：统一 Reranker → 原生 rerank 分数（词法只扩召回，专项测试证明）；同域纯 cosine → 原生排序；异构域 → 域内 RANK 共享名次 + 词法全局共享名次做 `61/2×(1/(60+domain_rank)+1/(60+lexical_rank))` 融合，其中词法证据对**所有入围父段**统一计算（`_final_lexical_ranks` 全局排名，不偏向词法 top C 命中者，无词法命中项词法项记 0）。异构且完全无词法证据时 `heterogeneous_without_lexical_evidence=true` 照常返回。
- HTTP/前端投影：`gateway.py` base create/update/item 与 search 请求体接入 `retrieval_mode`（strict Literal，非法值 422）；诊断投影（`retrieval_mode`/`lexical_candidates`/`heterogeneous_without_lexical_evidence`）T7 已就位。前端 `knowledgeBaseItemSchema` 增加 `retrieval_mode`，Create/Update/Search input 类型同步，mock e2e 的 baseView 补齐默认值；hybrid 模式选择/展示 UI 归 T11/T12。
- 测试：新增 `test_lexical_tokenizer.py` 14 例（识别顺序矩阵、全半角/大小写、无效 IP 回退、标识符部件、URL/标点切分、重复词元、零词元、>128B 哈希与封闭字母表防碰撞、256KiB 超限拒绝、v1 快照）；新增 `test_lexical_retrieval.py` 13 例（词法翻盘低 cosine 精确命中、纯语义不建词法查询、请求级覆盖不落库、强制 semantic 跳词法路、>128 词元拒绝、零词元回退向量、stale lexical_version 冲突（hybrid 域与融合入围两处）、词法新增项过 cosine 阈值、统一 Reranker 只扩召回、RRF 合并保词法赢家、parent_child 回卷取全 Child 最大 cosine、入围父段统一词法证据）；`test_ingestion.py`/`test_governance.py`/`test_reembedding.py` +4（发布/人工增改/Child 重切维护词法字段、reembed 不动）；`test_upload.py`/`test_retrieval.py`/`test_bases.py` +3（HTTP round-trip 与 422、服务级 round-trip 与非法值）。
- 门禁：`tests/knowledge` 全量 645 passed（真实 PostgreSQL）；前端 `tsc --noEmit` 与单测套件干净；`make format` 干净。质量评测（F09/F10 最终验收）按计划归 T14。

## 阶段 D：前端工作区

### T9：URL 状态、文档搜索和分页

依赖 T1；可在后端最终契约冻结后准备，不先发布。

- [x] 新增 navigation 纯解析/构造函数，保留现有 Project shell/client；URL 字段白名单为 kb/view/doc/segment/status/sort/page。
- [x] 用 push 表示资源前进/返回、replace 表示筛选/分页；文件关键词和检索/metadata内容不进URL或持久浏览器存储。
- [x] 在完整权威列表上关键词/状态过滤、稳定排序及20条分页；不能把默认500配额当成不检查分页完整性的理由。
- [x] 修正listAllPages提前空页/触顶仍成功返回部分数据的分支：未满足total时返回明确不完整错误，不用部分列表进行全量筛选或覆盖已有完整缓存。
- [x] 当前页选择、切页/筛选清空、删除末页回退；恢复列表关键词只限当前scope内状态。
- [x] 直达Segment通过详情读取定位，不遍历整个库；跨库ID组合/已删资源不得恢复旧对象。

测试：新增 navigation/document-list 单测，扩展 `document-cache-authority.test.ts`、Project scope 测试及 mock E2E。覆盖超过100条跨后端页筛选、提前空页/触顶不完整、刷新、浏览器前后退、非法UUID/页码、相同slug不同Project、失权/删除和乱序读取。

落地记录（2026-08-30）：

- 纯函数层：`core/knowledge/navigation.ts` 提供 `parseKnowledgeNavigation`/`buildKnowledgeSearch`（白名单 kb/view/doc/segment/status/sort/page，UUID 规范化、层级依赖裁剪、默认值省略、固定字段顺序）；`core/knowledge/document-list.ts` 提供 `deriveKnowledgeDocumentList`（关键词大小写不敏感匹配 name/original_name、状态过滤、四种稳定排序含 id tie-break、20 条分页与越界钳制）。
- listAllPages 完整性：提前空页或触达页数上限仍未凑齐 total 时抛 `INCOMPLETE_LIST`（新错误码 + i18n 文案），不再把部分列表当完整结果发布；React Query 缓存里已有的完整列表不被部分结果覆盖。
- 组件接线：`project-knowledge-page.tsx` 全面改为 URL 驱动（useSearchParams + push/replace 语义：进入/退出资源用 push，筛选/分页/关闭定位用 replace）；不可达 kb 显示明确错误并可返回列表；`knowledge-documents-view.tsx` 增加搜索框（仅本地 state，不进 URL）、状态筛选、排序、分页工具栏；文档查询错误优先回落到表格的阻断错误渲染，不误报“文档不存在”。
- Segment 定位：`useKnowledgeSegmentLocate` 经 segment detail 端点单点读取（不遍历库），`SegmentLocateCard` 呈现定位/过期/不可达三态，跨库或已删 ID 不恢复旧对象。
- 门禁：前端 `pnpm check` 干净；单测 1104 passed（新增 navigation 14 例、document-list 9 例、list-completeness 4 例）；mock E2E 82 passed（新增 6 例：URL 前后退/刷新、跨后端页筛选排序分页、不完整列表阻断、不可达 kb/doc、末页删除回退、segment 深链定位与异库失败）。

### T10：预览文件选择

依赖 T1。

- [x] 向导选择任一已选 File，切换时仅自动预览一次；参数编辑标stale，显式刷新，不实时重复上传文件。
- [x] 预览身份含File对象、参数、scope generation和请求序号；删除/替换文件、快速A→B、重新提交旧参数时仍不能被迟到响应覆盖。
- [x] 文件名、展示数/总数、父子关系、加载/失败/过期明确可见；失败不继续展示成当前有效预览。

测试：新增 preview identity 单测，扩展 `project-knowledge.spec.ts`；构造A慢B快、同文件参数变更、移除文件、scope切换、错误刷新。replay 验证所选文件预览与实际摄取一致。

落地记录（2026-08-30）：

- 纯身份层：新增 `core/knowledge/preview-identity.ts`——`KnowledgePreviewIdentity`（File 对象 + 参数快照 + scopeKey + 单调请求序号）与 `knowledgePreviewReducer`；响应只有在其身份仍是最新（同 scope、同 sequence）时才发布，被替换请求（快速 A→B、同参数重提交、移除/同名替换文件、scope 切换）的迟到响应一律丢弃；失败清空 payload，不得冒充有效预览。
- 向导接线：`knowledge-create-wizard.tsx` 预览面板加入 File picker（多文件时可选任一已选文件），仅保存当前文件的预览；每个新展示的 File 对象只自动预览一次（ref 防 strict-mode 双触发，参数无效时推迟到参数修复后触发一次）；参数编辑仅标 stale，显式刷新才重新上传；请求走 `mutateAsync` 且由 reducer 序号守卫收编迟到 settle。移除/同名替换文件即刻 dispatch 清理，scope 变化整面板清空。
- 可见性：新增 `previewShowing(count, total)`（展示数/总数）与 `previewPickFile` 文案（中英），`previewHint` 不再限定“第一个文件”；加载/失败/过期沿用 role=status/alert 呈现。
- 门禁：新增 preview-identity 单测 15 例（迟到覆盖、重提交、移除、跨 scope 序号碰撞等）；mock E2E 新增 2 例（picker 每文件仅一次自动预览与回切重新请求、慢响应不覆盖胜者与移除清空），全套 84 passed；`pnpm check` 干净；real-backend `knowledge-real-backend.spec.ts` 8 passed（含所选文件预览与实际摄取分段字节一致断言）。

### T11：检索结果详情和诊断

依赖 T5/T7/T8/T9。

- [x] 结果显示本次 score_kind 与最终排名，原生阈值分、实际模型/参数/数量/耗时置于折叠诊断；不显示“可信度百分比”。
- [x] 原分段详情和匹配Child从本次结果/受权威读取取得；过期提示重新检索，历史引用保留原摘要。
- [x] 结果进入维护页和返回定位闭环；跨库、重建、参数变化、换Reranker后清理旧结果/详情；新请求胜出，迟到响应不能恢复旧结果。
- [x] 从未测试、无命中、过滤为空、未ready、内容过期、模型失败分别呈现；错误在结果区持续可见并允许重试。
- [x] 桌面并排、窄屏堆叠；键盘打开/关闭详情、焦点返回、错误关联和loading可访问性完整。

测试：扩展 citation/knowledge 单测、mock E2E；real-backend 验证parent_child真正命中非首Child、完整原段、版本变化后409、不同评分分支及无内容泄漏，不能由详情Child列表回推命中。

#### T11 落地记录（2026-08-30）

- API/hook 接线：`api.ts::searchKnowledge` 透传 `retrieval_mode`（仅显式覆盖时入体）；新增 `useKnowledgeSearchHitDetail`（`gcTime: 0` 不缓存详情，带 `expected_document_version`/`expected_content_digest` 走 T5 的旧检索详情语义，任何漂移即 409）；`knowledge-base-detail.tsx` 传 `onLocateSegment` 把详情定位接到 T9 URL 导航（`view=documents&doc=…&segment=…`），返回走浏览器历史，闭环成立。
- 面板重写（`knowledge-search-panel.tsx`）：桌面 `xl` 两列（表单/结果）、窄屏堆叠。检索测试固定 `debug: true`（诊断仅存在于本次响应，不落日志）；新增本次检索模式覆盖 Select（default/semantic/hybrid，不写回 Base）。结果行显示最终排名 `#n`、`score_kind` 徽标（Cosine/Rerank/Rank fusion，无"可信度百分比"文案）；折叠 `<details>` 诊断含策略版本/模式/预算/实际计数（semantic/lexical/dedup/threshold/stale/returned）/单调耗时/模型 ID/逐 hit 局部分数与 matched_children——只有分数与身份，绝不含正文。`baseConfigKey`（embedding/reranker/mode/defaults/updated_at）变化即 `search.reset()` 并关闭详情；提交按钮 pending 禁用 + React Query mutation 只跟踪最新调用，配合组件随视图卸载，迟到响应无法恢复旧结果（专项 mock E2E 用手动释放的慢响应证明）。
- 空/错误态正交：从未测试（`knowledge-search-never`）、无命中（`no_candidates`）、过滤为空（`filtered_out`）、未 ready（`not_ready`）、内容过期（`stale_candidates`）各有独立文案；模型失败保持错误面板持续可见，Retry 按原 `lastInput` 重发。
- 详情对话框（`SearchHitDetailDialog`）：从受权威读取取完整原段（非 snippet），钉住检索时的 version/digest——内容漂移显示"重新检索"冲突提示而非静默换内容；matched children 单独列出并在 Child 分页列表中打 Matched 徽标（证据来自本次响应 `hit_diagnostics`，不由 Child 列表回推）；"Open in documents" 定位维护页。Radix Dialog 提供键盘开关/焦点返回；错误/加载态有独立可见呈现。
- mock E2E（`project-knowledge.spec.ts` +4）：rank+score_kind+折叠诊断安全性（含 `retrieval_mode` 覆盖入参与诊断不泄漏正文断言）；四种空因区分 + 模型失败持续可见并可重试；详情钉住内容/真实 matched Child 高亮（分页第二页）/409 冲突/定位闭环；慢响应在改绑 Reranker 后落地不得复活旧结果。既有"换 Reranker 清结果"用例继续覆盖清理语义。
- real-backend（`knowledge-real-backend.spec.ts` 扩展 2 例）：无 Reranker 搜索行带 Cosine 徽标、绑定后带 Rerank 徽标（不同评分分支）；parent-child 命中详情含完整两行原段（含 snippet 截不到的行尾）、两个真实命中 Child 均高亮（含非首 Child，marker 轴证据）、诊断展开含计数但无任何段正文；通过 API 直接改段内容后重开详情命中 409 冲突提示（version/digest 漂移）。
- 门禁：`pnpm check` 干净；unit 全绿；`project-knowledge.spec.ts` 41 passed；real-backend 改动的 2 例 passed（临时 PG + MinIO + replay Worker）。

### T12：进度、元数据与重处理操作

依赖 T2–T4/T6/T9。

- [x] 逐文件上传结果继续保留；汇总处理中/成功/失败/等待自动重试，不能把全部终态当成成功。
- [x] 显示安全的真实阶段/计数/尝试，未知总数不定进度；旧attempt进度不得倒灌；deleting错误保留现有停轮询规则。
- [x] 批量metadata只操作当前选择，显示混合值/保持/设置/清空和覆盖数量，一次共同patch；只读成员仅查看定义。
- [x] 库设置显示独立的重嵌入操作；文档处显示重新解析操作，改参数可先预览，确认明确保留/覆盖、费用和暂不可检索范围。
- [x] 服务端冲突保留未保存表单，刷新权威信息后让用户重新确认；401/403/404清除失权数据，不在新scope恢复旧表单。

测试：mock E2E 全失败、部分失败、重试中、迟到进度、批量混合值/全批回滚、reparse冲突；real-backend 重嵌入后UUID/文本/启停保持，reparse才替换。

#### T12 落地记录（2026-08-30）

- 任务进度 UI（`knowledge-documents-view.tsx`）：状态单元格在徽标下渲染 `TaskProgressLine`（testid `knowledge-task-progress`）——`kind · stage`（失败时 `Failed during <stage>`）、`completed/total`（`total_units` 为 null 不定进度、不显示计数）、`Attempt n/max`（首次尝试且非 retry_wait/failed 不显示）、retry_wait 展示 `next_attempt_at` 本地时间。进度数据来自 T4 的服务端投影（仅当前代次、succeeded 不投影），旧 attempt 进度结构上无法倒灌；新 attempt 从服务端归零后的行重新渲染。文档表上方新增处理汇总条（testid `knowledge-processing-summary`）：处理中/等待自动重试/失败（destructive 强调）/就绪四类计数，全终态时只报就绪与失败，不把失败折进成功。deleting 停轮询规则未触碰。
- 上传逐文件结果：既有对话框逐文件 verdict/失败保留/重试不重传逻辑保持；mock 上传 handler 新增按文件名 `reject` 注入 413 配额失败（`acceptRejectedUploads` 开关恢复），新增 E2E 覆盖部分失败（1 成功 2 失败、服务端 message 原样呈现、重试仅重发失败者）、全失败持续可见、恢复后重试清队并关闭对话框。
- 批量 metadata（`BatchMetadataDialog` + `useSetKnowledgeDocumentsMetadata` + `PATCH /bases/{id}/documents/metadata`）：仅作用当前勾选行；逐字段 Keep/Set/Clear 三态，混合值显示 `n distinct values` 且 Set 时提示覆盖数量；仅显式编辑的字段进入一次 all-or-nothing patch（T6 服务端整批回滚保证）；409 冲突保留表单值并刷新权威文档缓存后由用户重新确认；只读成员无批量入口（`canEdit` 门）。字段定义来自 metadata-fields 权威读取。
- 重处理入口：库设置的重嵌入区文案改为 Re-embed（按钮/确认对话框明示保留段文本、人工编辑、启停状态，仅重算向量、处理期间不可检索、产生模型费用），提交后展示真实准入结果 `accepted_document_count`/`skipped_document_ids`（testid `knowledge-rebuild-outcome`），未发布文档被跳过时明确提示走 retry 原文件解析。文档行动作菜单新增 "Reparse from original"（ready/failed 可用）：`ReparseDocumentDialog` 预填该文档当前 8 个解析参数，改参数即预览失效（`preview.reset`），`Preview split` 走服务端 `reparse-preview`（不落库），确认按 `expected_version` CAS 提交；409 保留参数表单、失效预览并刷新权威版本后重新确认。
- mock E2E（`project-knowledge.spec.ts` +4，全套 45 passed）：进度矩阵（4 文档并行走 stage/计数/attempt/retry_wait/未知总数/失败阶段 + 汇总条计数 + 新 attempt 归零）；批量 metadata 混合值/三态/仅提交编辑字段/409 保留表单重确认；reparse 预填/预览/参数变更失效/版本冲突刷新后重提交成功；多文件上传部分失败/全失败/重试。rebuild 用例改为断言 Re-embed 文案与 accepted/skipped 结果。
- real-backend（`knowledge-real-backend.spec.ts` 8 passed，临时 PG + MinIO + replay Worker）：重建用例升级为完整生命周期——API 快照发布段（UUID/内容/enabled）→ 人工改段2 + 禁用段1 → UI Re-embed（outcome 报 1 accepted）→ provider embedding 调用增长 → ready 后断言 UUID/位置逐一相等、编辑内容保留、禁用保持，编辑段检索可命中（新代次向量真实服务）→ Reparse 对话框改 chunk_size=500 预览后确认 → 新行 UUID 与旧集合零交集、人工编辑消失、启停重置、marker 段落恢复且检索命中、旧编辑内容检索不可见。故障注入用例补断言真实进度行（`Failed during Embedding` + `Attempt 3/3`）；`Documents` 导航选择器全部改精确匹配（与新 Re-embed documents 按钮消歧）。
- 门禁：`pnpm check` 干净；unit 全绿；`project-knowledge.spec.ts` 45 passed；`knowledge-real-backend.spec.ts` 8 passed。

## 阶段 E：验证与交付

### T13：确定性全链路与安全门

依赖 T1–T12。

- [x] 扩展 replay Provider：支持可控分段向量、两种评分域、分批失败/延迟及调用计数。fixture 不调用外部模型，固定ID/时间。
- [x] 临时 PostgreSQL + MinIO + replay Worker 完成：上传 → 指定文件预览 → ready → 搜索 → 原段定位 → 人工编辑/禁用 → 重嵌入保持 → 原文件重解析覆盖 → 检索验证。
- [x] 同时覆盖字段发现/批量metadata、查询owner retention、Project删除/恢复、失租和旧响应；先明确失败归属，不扩大到其他域改造。
- [x] 单独验收 Agent真实工具装配与 ToolMessage 历史引用、正文尾部答案、budget omitted_count、已有 Provider guard，没有新增权限或明文材料泄漏。
- [x] 实际执行数量/失败/跳过逐项记录，不将mock浏览器当真实后端，也不将replay模型当真实质量。

#### T13 落地记录（2026-08-31）

- replay Provider（`backend/tests/replay_knowledge.py`）：确定性合同保持——marker 轴向量（可控分段向量：marker 文本落轴 0 带 0.05 轴 1 分量，其余落轴 1）与两种评分域（cosine 域=无 Reranker、rerank 域=marker 0.95/其余 <0.6，绑定/解绑即切换分支）。本任务新增 `rerank_failures` 故障注入（与既有 `embedding_failures` 同口径，`POST /provider/faults` 整体替换故障状态、响应含双计数与双剩余）；调用计数既有。分批失败/延迟语义的归属：批级失败、退避与迟到进度由后端 `test_task_progress.py`（async MockTransport 逐批控制）覆盖，浏览器侧迟到响应由 mock E2E 手动闸门（慢搜索释放）覆盖，replay Provider 不再重复实现延迟注入。fixture 固定 UUID5 模型/Provider ID，不调用外部模型。
- 全链路（`knowledge-real-backend.spec.ts` 9 passed，临时 PostgreSQL + MinIO + replay Worker，含 `pnpm build && pnpm start` 生产前端）：向导两文件上传 + File picker 指定第二个文件预览（诱饵文件内容不得漏入，预览与实际摄取分段逐字节一致）→ ready → 搜索（cosine/rerank 双分支 + 分数徽标）→ 命中详情完整原段 → **Open in documents 定位闭环（URL 携带 doc/segment、定位面板钉住同一真实内容）** → 人工编辑/禁用（API PATCH）→ 重嵌入保持（UUID/位置/内容/enabled 逐一相等 + 编辑段新向量可检索）→ 原文件重解析覆盖（新行零交集、编辑消失、启停重置、chunk_size=500 生效）→ 检索验证（marker 回归命中、旧编辑不可见）。
- 故障与恢复（同套件）：embedding 故障耗尽三次尝试 → 文档 failed 且状态单元格带真实阶段/尝试证据（`Failed during Embedding`、`Attempt 3/3`）→ 清故障 retry 到 ready；rerank 故障 → 检索错误持续可见（绝不冒充空结果）→ 清故障后 Retry 原查询成功。字段发现/批量 metadata real-backend：建字段、单文档赋值过滤命中、批量对话框混合值（2 distinct values）→ Set 统一值 → 相同 equals 过滤两文档同时命中（写真实到达 JSONB 召回 SQL）。
- 后端确定性域证据（`tests/knowledge` 645 passed，随机隔离库 core gate runner，`-m "not provider_integration"`）：字段发现/批量 metadata（`test_metadata.py`）、查询 owner retention（`test_query_retention.py` 含 Project purge 清全 owner 历史）、Project 删除/恢复（`test_tasks.py` purge 幂等/defer 近期上传/版本化 bucket 拒绝，`test_worker.py` pending 暂停与恢复后执行、purge 等待 running 任务、restore 在 purge job claim 期间被拒、retention purge 先于治理提交且 fail-closed）、失租与旧响应（`test_task_progress.py` 失租停批+晚到进度不落地，`test_reembedding.py` 失租迟到 no-op）。
- Agent 工具单独验收（`test_agent_tool.py`，在 645 内）：真实 lead agent 双工具装配、ToolMessage 历史引用带新引用字段、320/4000 字符正文尾部答案完整送达、64KiB budget 跳过与 omitted_count、首段超预算稳定错误、错误 ToolMessage 透传（含缺能力 `KNOWLEDGE_FORBIDDEN`）；Provider guard 与撤权（`test_authority.py`、`test_models.py` guard 时序/批间停派）。诊断/工具输出无正文泄漏由 `test_retrieval.py` debug 用例与 real-backend 诊断断言共同覆盖。
- 失败归属（T13 要求先归属再处置）：全量下 `test_run_skill_tree_orphan_reaper.py::test_reaper_deletes_only_inactive_proven_owners_and_is_idempotent` 两次复现失败而单文件连续 3 次通过。归属定位：干净基线 worktree（M9 交付 commit `063b345b` + 同一运行配置）全量 5015 passed 全绿，本分支该域产品与测试文件零改动——失败非既有也非 M10 产品缺陷，而是该测试自身的跨时钟源竞态被 M10 新增的 126 个后端测试改变全量时序后暴露：`grace_seconds=0` 时 reaper 以 PostgreSQL `clock_timestamp()` 对比 owner 元数据里进程时钟写入的 `updated_at`，亚毫秒偏差即把新建 owner 挡进 `preserved_grace`，`preserved_unknown` 断言失败。处置为最小测试侧修复（reap 前 `asyncio.sleep(0.1)` 抵消跨时钟偏差，附注释），未触碰任何产品代码；修复后单文件 3 次与全量回归验证通过。
- 另一偶发观察（记录备查，未处置）：中途一次全量（18:05Z，测试集尚为 5139 条时）`test_tool_call_control_scope_checkpoint_acceptance.py::test_materialized_checkpoint_replay_preserves_controlled_batch_and_hard_limit[delta]` 失败一次；该域产品与测试文件本分支零改动（文件最后改于 2026-08-29），单文件双参数通过，其余六次全量（含基线 worktree 两次、最终门禁与事后复现各一次 5168 全绿）均通过，未能复现。归类为该测试自身的罕见时序敏感，与 M10 无关；若再现需按 T13 归属流程先取回完整 traceback（勿再用 `tail` 截断）再处置。
- 门禁执行记录（本机 macOS，2026-08-31）：backend `tests/knowledge` 645 passed / 0 failed / 0 skipped（随机隔离库 core gate）；backend 全量 `uv run pytest tests` 5168 passed / 4 skipped / 0 failed（上述时序修复后）；`make lint`（ruff check + format --check，1285 文件）干净；`generate_schema_comments.py --check` 108 表 / 1352 列一致；frontend `pnpm check`（eslint+tsc）干净、单测全绿、`project-knowledge.spec.ts` 45 passed、`knowledge-real-backend.spec.ts` 9 passed、`pnpm build:production` 成功。mock 浏览器套件与 replay 模型仅作确定性证据，不替代真实后端质量（真实质量门归 T14）。

候选执行命令（从标注目录运行；只在具备对应测试前提时执行）：

```bash
# backend/：核心runner只在随机隔离库执行测试DDL
PYTHONPATH=. uv run python tests/support/core_gate_plugin.py tests/knowledge/ -q -m "not provider_integration"
make test
make lint
uv run python scripts/generate_schema_comments.py --check

# frontend/：不同Playwright模式分开
pnpm check
pnpm test
pnpm exec playwright test tests/e2e/project-knowledge.spec.ts
pnpm exec playwright test --config playwright.real-backend.config.ts tests/e2e-real-backend/knowledge-real-backend.spec.ts
pnpm build:production
```

实现变更还须按 owning guide 执行格式化和受影响的边界门。`make check-db` 是指定目标的只读 readiness，不代替空库 Schema 契约；不把 `make reset-db` 放进普通测试/启动命令块。临时服务使用现有 replay 测试启动方式，不能指向用户业务库试错。

### T14：真实质量、性能与最终文档

依赖 T13。

- [x] 提交脱敏父段三级标注样本、稳定来源及计算方法；至少60条，分层冻结开发集与独立验收集（至少30题，其中标识符至少20题），覆盖设计方案第11节类别。真实模型调用事先确认使用条件和预算。
- [x] 记录M9/M10、semantic/hybrid、同域/异域、无词法证据四类对照；按方案阶段分别报告实际候选数、Recall@candidate、最终Recall@10、nDCG@10、P95、调用条目数和费用；无答案IDCG=0样本单报误召回，不混入普通均值。
- [x] 执行设计方案门槛，特别是标识符候选/最终Recall均≥95%、自然语言召回/nDCG及无答案回归限制、P95回归复审。纯语义异构域若变差，不能用“秩融合已统一分数”解释为通过。
- [x] 调参只用开发集；验收失败须记录原因、修正并重新组织独立验收，不能用同一验收集反复选参数。若要取消/延后F09或F10，须先修订方案范围并取得确认，不能自行标记M10完成。
- [x] 更新 README、Install、backend/frontend AGENTS、知识库需求/架构/摄取/检索文档及总计划；明确重嵌入/重解析、模型正文/短引用、原生阈值/最终排序、真实进度与部署约束。需要新增领域词条时按仓库词汇流程处理，不恢复旧 Knowledge Model Configuration。
- [x] 确认操作者目标库、停服/数据处置及新版本Schema readiness；本计划本身不是reset授权。旧数据必须保留且无受支持升级路径时，部署仍阻塞。

#### T14 落地记录（2026-08-31）

- 文档同步（此前已完成）：README / backend+frontend AGENTS / 需求·架构·摄取·检索设计按 M10 行为更新；Install 与模型接入层无新约束不改；MVP 执行计划 M10 小节在部署确认前保持"计划中"。
- 语料：`backend/tests/knowledge/fixtures/m10_retrieval_cases.json`（生成器 `_generate_m10_eval_corpus.py`）。合成脱敏手册，无 PII；标注单位为父段，身份键 `source_id+position+SHA-256(content)`，不用数据库 UUID。65 题分层冻结（dev 26 / holdout 39，holdout 标识符 22），覆盖标识符、中文自然语言、段尾答案、父子回卷、大库+小库/异构、metadata、无答案。契约测试 `test_m10_eval_corpus.py` 16 例锁规模与计算方法。
- 真实模型（操作者已授权）：隔离空库 + SiliconFlow。主嵌入 `Qwen/Qwen3-Embedding-8B`（1024 维；生产默认 VL 8B 同系列文本嵌入，固定 dimensions 以适配客户端契约）、副嵌入 `Qwen/Qwen3-Embedding-0.6B`（异构域；`bge-m3` 拒绝客户端固定发送的 `dimensions`）、重排 `Qwen/Qwen3-Reranker-8B`。规模 10002 检索单元。M9 等价路径 = 同模型 `retrieval_mode=semantic`（未另起 M9 worktree，因词法/分库预算只存在于当前代码；语义路即 M9 原生排序）。130 次检索，embed 195 条 / rerank 5960 篇，估费约 ¥0.07。报告：`docs/knowledge/m10-quality-eval-report.md` 与同名 JSON。
- 验收集门槛（未在验收集上调参）：标识符 hybrid Recall@candidate = 1.0、Recall@10 = 1.0（门槛 ≥0.95）；M9 语义标识符 Recall@10 = 1.0。自然语言两种召回与 nDCG@10 均为 1.0，相对语义基线下降 0（门槛 2pp / 0.02）。无答案误召回 0，不混入均值。段尾答案 hybrid 零漏项。异构域（含无词法证据的 semantic）Recall@10 = 1.0，未出现"分数统一但变差"。
- P95 复审：自然语言非 Provider P95 semantic 98.8ms → hybrid 536.8ms（约 5.43×，超过 50%）。归因是 1 万单元上的 GIN/tsquery 词法路，不是 Provider。按方案作产品预算复审并接受该代价，不回调参、不降规模、不虚构 500ms 承诺。
- 部署确认（2026-08-31）：操作者明确"可以随时重置数据库"——旧数据无保留要求、停服窗口不受限。目标库为根 `.env` 的开发库（本机 PostgreSQL 17，库名 `deerflow_knowledge`）；已执行 `make reset-db`（`CONFIRM_DATABASE` 精确库名确认，bootstrap 预检通过；模型供应商 bootstrap Key 由会话内映射既有 SiliconFlow Key 提供，建议将 `.env` 内旧变量名补为 M9 的 `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY`），随后 `make check-db` 取得只读证据：schema_v1 ready、vector/pg_trgm 已安装；并抽查确认新库含 M10 列（`retrieval_mode`/`published_version`/`lexical_tsv`/`reparse_settings`/`top_score_kind` 5/5）。至此 T14 全部完成，M10 总状态更新为"已完成"。

#### 交付后审查修复记录（2026-08-31）

以 M9 交付 commit `063b345b` 为基线的全量代码审查确认实现完整、无严重级缺陷；三个中等缺陷已按 TDD 修复：

- 后端锁序：`rename/delete_metadata_field` 的批量键改写先按 UUID 序 `SELECT … FOR UPDATE` 锁定受影响文档再 `UPDATE`，消除与批量启停/删除（UUID 序锁行、不取 Base 锁）之间的死锁窗口；新增真实 PostgreSQL 锁序编排测试（`test_metadata.py`，修复前稳定复现 deadlock detected）。
- reparse 预览冲突：预览路径 409 后现与提交路径一样失效文档列表缓存，冲突提示改为粘性状态（权威刷新后仍可见，直到用户再次预览/提交），文案承诺的"已刷新"自此属实。
- metadata 冲突刷新：单文档/批量赋值 hooks 在 409 或选择对象已消失（404）后失效文档列表缓存；单文档对话框改为按 id 读实时行（行消失自动关闭），批量对话框的选择随权威刷新收缩后再确认。新增 3 个 mock E2E（预览冲突刷新、批量选择收缩重确认、单文档行消失关闭）。

同时修正本文 T8 落地记录与代码不符的表述（lower、`x`/`h` 编码前缀与 128B 阈值、256KiB 超限拒绝、`ts_rank_cd` 归一化 flag=2、RRF 无 0.5 系数、tokenizer 用例数）及评测报告的部署阻塞表述（阻塞已于 2026-08-31 解除）。

## 依赖与并行安排

- T0 → T1 是共同前置。
- 内容保护链：T2 → T3 → T4；T5、T6可在契约冻结后并行准备，避免共同修改contracts/Schema时冲突。
- 检索链：T5/T6 → T7 → T8；与T2/T3在同一发布点联调词法字段。
- 前端纯状态工作T9/T10可与后端并行；T11等待检索最终DTO，T12等待任务/重处理/metadata契约。
- T13 → T14 为交付门；整个M10前后端与Schema同步发布，不能把任何“暂时只有后端”的中间态交付用户。

## 完成清单

- [x] F01–F10逐项有实现与验收证据，已具备的M8/M9能力未被重复实现或退化。
- [x] 没有模型正文截断、人工内容意外重建、旧版本向量写回、跨项目/owner泄漏。
- [x] 排名/阈值/日志/历史/UI语义一致；融合分不是置信概率，质量限制可见。
- [x] 所有确定性门通过，真实质量/性能门有有效结果；缺权限/数据时明确未完成。
- [x] Schema、DTO、装配、模型引用、retention、文档一致，已有数据库处置已单独确认。
- [x] M10总体状态最后才从“计划中”更新为“已完成”（2026-08-31，见 T13/T14 落地记录）。
