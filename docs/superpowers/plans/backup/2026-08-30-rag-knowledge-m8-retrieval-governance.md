# RAG Knowledge Package M8（检索与治理增强）Implementation Plan

> 状态：已完成（2026-08-30）。本文档为已落地实现的执行计划存档，
> 供后续维护与回溯；内部四批原编号 K1–K4，对应《RAG 知识库 MVP 执行计划》
> 的 M8 小节。K4 第四、五项（URL 单页导入、同名去重）未启动。

## 目标

以 Dify 1.17.0 知识库模块为参照，在不引入 Redis/Celery/Unstructured 的前提下，
把分段与文档治理、分块质量、检索质量和按需扩展（元数据过滤、模型重建、
新文件格式）迁移进 `actweave-knowledge`。异步一律走现有 `knowledge_tasks`
队列，同步预览走 Gateway 请求内计算；切分参数延续"上传时固化"原则。

## 文件范围

```text
backend/packages/knowledge/actweave_knowledge/
backend/packages/harness/deerflow/persistence/full_schema.sql
backend/packages/harness/deerflow/persistence/final_schema_contract.py
backend/packages/harness/deerflow/persistence/final_schema_digest.py
backend/packages/harness/deerflow/persistence/schema_comments.sql
backend/app/knowledge/gateway.py
backend/app/knowledge/run_tool.py
backend/scripts/generate_schema_comments.py
backend/scripts/check_postgres.py
backend/tests/knowledge/
frontend/src/core/knowledge/
frontend/src/core/i18n/locales/
frontend/src/components/projects/knowledge/
frontend/src/components/workspace/citations/
frontend/tests/unit/core/knowledge/
frontend/tests/e2e/project-knowledge.spec.ts
frontend/tests/e2e-real-backend/knowledge-real-backend.spec.ts
```

## Task 1：治理 Schema 与后端（第一批）

- `knowledge_segments` 增加 `enabled`（默认 true）与 `word_count`；
  `knowledge_documents` 增加 `enabled` 与 `word_count`；
  ORM、`full_schema.sql`、catalog digest、中文注释、Schema 测试同批次改。
- 检索候选查询追加 `document.enabled AND segment.enabled` 过滤；
  禁用不删除向量，重新启用即恢复可检索。
- 分段编辑/手工新增/删除 API：仅 `ready` 文档可操作；事务内校验 Document
  version，编辑与新增用同库模型配置重算 embedding 后同批写入；
  version 竞争返回 `KNOWLEDGE_CONFLICT`；手工段 `source_position` 标记
  manual 并执行 `max_segments_per_document` 配额检查。
- 文档重命名（只改 `name`）、批量启停/删除（全有或全无、有界批量）。
- 摄取统计段字数并聚合到文档行。

## Task 2：治理前端（第一批）

- 文档详情从"分段预览弹窗"升级为库内分段浏览页：列表、启停开关、
  编辑抽屉、手工新增、删除、字数与 manual 徽标；只读成员无操作控件。
- 文档列表增加启停开关、重命名、字符数列与批量操作条；
  `deleting` 行不可选。

## Task 3：切分器与预处理（第二批）

- 切分器升级为递归分隔符切分：上传可选自定义分隔符（转义形式，默认
  `\n\n`），回退序列 `["\n\n", "\n", "。", ". ", " ", ""]`（与 Dify 一致，
  含行边界回退），chunk_size/overlap 语义不变。
- 预处理规则 `remove_extra_spaces`（压缩连续空白与三个以上换行）、
  `remove_urls_emails`（URL 正则只匹配可打印 ASCII，遇 CJK 即停，
  修复 Dify 同款吞字缺陷）；作为上传参数存入 Document 行，默认关闭。
- 全部分块参数上传时固化，重试回读行内参数保证结果一致。

## Task 4：分块预览（第二批）

- Gateway 同步分块预览 API：抽取→清洗→切分共享同一通道，返回前 10 块
  与总段数；复用上传大小/格式校验，请求结束清理临时文件，不写库不入队。
- 创建向导第二步接入防抖实时预览面板，调整参数即时刷新，
  解析失败展示原因；上传对话框与向导暴露同一组参数控件。

## Task 5：父子分块摄取（第三批）

- 上传新增模式 `general | parent_child`；父块承载返回内容，子块按父块内
  二级切分（独立 child chunk size/separator 参数）承载向量。
- 新表 `knowledge_segment_children`（父块行向量列置空）：父块先 flush
  再插子块，同一事务发布、version 校验防旧任务覆盖、删除级联。
- 分段治理在 parent_child 文档上编辑/新增时重切子块并嵌入子块。
- 预览 API 在 parent_child 模式下按父块嵌套返回子块内容。

## Task 6：检索回卷、库级默认与查询日志（第三批）

- 召回合并两条路径：general 段用自身向量；parent_child 文档经子块召回，
  子块最高分回卷父块（每父块一个候选、去重）后进 Reranker。
- `knowledge_bases` 增加 `default_top_k` 与 `default_score_threshold`；
  检索测试与 Agent `knowledge_search` 未显式传参时在服务端解析库级默认。
- 新表 `knowledge_queries` 记录 project、目标库、query、来源
  （agent/retrieval_test）、结果数与最高分；分段 `hit_count` 命中自增
  并聚合到文档，均为 best-effort 副作用。
- Reranker 失败保持 `KNOWLEDGE_RERANK_FAILED`，不静默回退 cosine-only。

## Task 7：检索质量前端（第三批）

- 向导与上传对话框增加模式选择与子块参数（仅 parent_child 模式渲染并
  随请求下发）；向导预览嵌套展示子块，步骤三摘要含模式行。
- 库设置面板增加检索默认参数；检索测试 top_k/阈值输入留空即省略字段、
  placeholder 展示库级默认。
- 检索结果下方增加最近查询表（分页、来源徽标、点击回填输入框），
  每次完成搜索后失效刷新。

## Task 8：元数据字段与文档元数据（第四批）

- 新表 `knowledge_metadata_fields`（库内字段名唯一，类型
  string/number/time）；`knowledge_documents` 增加 `doc_metadata`
  JSONB 列（默认 `{}`，GIN 索引）。
- 字段 CRUD：创建校验名称长度与每库上限，重名返回
  `KNOWLEDGE_NAME_CONFLICT`；重命名/删除同事务用 JSONB 算子批量改写
  文档键（`-`/`||`/`jsonb_build_object`）。
- 文档元数据赋值 API：按字段定义校验类型化取值（string 限长、number
  有限、time 为 epoch 秒），`null` 删除键，未提及的键保持不变。
- 前端：库详情新增"元数据"二级菜单管理字段定义；文档行"元数据"
  对话框按类型渲染输入（text/number/datetime-local），已存值清空提交
  显式 `null`，未改动字段不发送。

## Task 9：元数据过滤检索（第四批）

- `KnowledgeSearchRequest` 增加 `metadata_filters`（name/operator/value，
  eq/contains/gte/lte，AND 组合，上限 10 条）；执行前按库字段定义校验，
  SQL 谓词用 JSONB 包含（eq）与 `jsonb_typeof` 保护的 CASE（range/
  contains），两条召回路径同时生效。
- Agent 工具接受 `list[dict]` 形式过滤条件并转换为包内 DTO，
  不完整条目返回业务错误不崩溃。
- 检索测试面板增加条件行 UI：字段 + 按类型给操作符（文本 eq/contains，
  数字与时间 eq/gte/lte）+ 类型化取值；无字段时提示。

## Task 10：模型重建（第四批）

- `POST /bases/{base_id}/rebuild`：同步换绑模型配置（校验存在且 active）
  后逐文档 version bump、状态置 `queued`、清错误并入队现有摄取任务；
  未新增任务类型，重试粒度落在单文档；旧任务因 version 不匹配 no-op，
  旧版本分段因 version 过滤自然退出召回。
- 解除"库绑定模型配置不可变"限制；deleting 库与并发配置删除返回
  稳定业务错误。
- 前端设置页增加"嵌入模型"重建区块：模型选择、确认对话框、
  进行中/已开始状态文案。

## Task 11：新文件格式（第四批）

- 新增 extractor：`.html/.htm`（BeautifulSoup4，剥 script/style，
  拒绝无可见文本）、`.pptx`（python-pptx，slide 溯源）、`.epub`
  （ebooklib，chapter 溯源，跳过导航文档）；沿用字符预算与
  zip 容器炸弹预检。
- 上传/预览校验扩展扩展名清单；前端 accept 与上传说明同步，
  源位置文案增加 slide/chapter。

## Task 12：浏览器验收

Mock Playwright 场景：文档启停/重命名/批量条/分段浏览页闭环；向导预览
实时刷新、错误恢复、上传冻结参数；父子向导嵌套预览与参数冻结；上传
对话框父子参数；最近查询列表/刷新/回填；库默认参数保存与留空下发；
字段管理与重名冲突；文档元数据保存与清空；过滤条件下发（含数值转换
与移除后不发键）；重建确认与文档重跑；新格式 accept。

Real-backend Playwright 场景（临时 PostgreSQL + MinIO + replay Provider）：
编辑后按新内容命中、禁用段/文档不再命中、重新启用恢复；预览分块与实际
摄取分段逐字节一致；两条 marker 子块命中回卷为单一父块引用、库级默认
top_k 生效；两份同内容文档仅靠元数据过滤命中其一；重建后 provider
嵌入调用增长、文档回到 ready、新版本分段可检索。

## 放行门

- backend `make format` 与 `tests/knowledge/`、Schema 契约套件通过；
- frontend `pnpm check` 与单测通过；
- mock 与 real-backend Playwright 分开报告并全部通过；
- 每批合并前 ORM、SQL 快照、digest、注释与 Schema 测试同批次一致；
- 更新《RAG 知识库 MVP 执行计划》M8 小节、`README.md` 与两份
  `AGENTS.md`；提醒已有数据库执行 `make reset-db`（破坏性操作）。
