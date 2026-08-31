# RAG Knowledge M11（查询向量缓存、分段摘要索引、知识库系统设置）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（推荐）
> 或 superpowers:executing-plans 按任务推进本计划，步骤用 `- [ ]` 勾选跟踪。
> 状态：立项（2026-08-31），未实施。
> 规范来源：[M11 设计方案](../specs/2026-08-31-rag-knowledge-m11-design.md)。
> 前置：[M10 计划](2026-08-30-rag-knowledge-m10-quality-workbench.md)完整验收；基线 commit `b94d8b34`。

**Goal**：交付 F01 查询向量缓存、F02 分段摘要索引、F03 知识库系统设置（配置入库 + 管理页），
全部确定性门与真实质量门通过。

**Architecture**：包（`actweave_knowledge`）继续宿主无关——缓存与摘要业务在包内，LLM 调用经
`KnowledgeModelPort` 新端口方法进宿主 harness `ModelRuntime`；配置面是宿主域（新单行表 +
admin 路由 + 启动装配），包只消费 `KnowledgeSettings` DTO。两张新表都是 Schema V1 成员。

**Tech Stack**：现有栈不变（FastAPI/SQLAlchemy async/pgvector/MinIO/Next.js/Zod/Playwright），
无新依赖。

## 全局约束（每个任务隐含）

- 工作树现存与知识域无关的未提交改动（README、design-qa、frontend projects/workspace、
  harness runtime 等）：**不得触碰、staging、还原**。i18n 三文件（en-US/zh-CN/types）当前也有
  用户改动，M11 只做增量 key 追加，提交时只暂存自己的 hunk。
- Schema 五件套同批：ORM、`full_schema.sql`、`final_schema_contract.py`（含
  `FINAL_SCHEMA_V1_CATALOG_SIGNATURE` 计数/digest）+ `final_schema_digest.py`、
  `schema_comments.sql`（`generate_schema_comments.py` 重生成并同步 `_EXPECTED_TABLE_COUNT`/
  `_EXPECTED_COLUMN_COUNT`）、聚焦 schema 测试。另须同步
  `scripts/check_postgres.py::REQUIRED_TABLES` 与（包表）`final_schema_contract.KNOWLEDGE_APP_TABLES`。
- Provider 调用（LLM/Embedding/Rerank）不持数据库事务；每个真实批次与客户端内部重试前跑
  guard；发布单事务内复验 claim/version/绑定。
- 密钥纪律：MinIO secret 走 `deerflow/secrets` envelope（AES-GCM，recipient 绑定），响应/日志/
  审计/repr/model_dump 永不出现明文；摘要 prompt 与召回材料不升级为系统指令。
- 部署门：受支持安装验证路径是全新空库；不执行 `make reset-db`，M10 的 reset 授权不延伸；
  临时测试库与操作者目标库严格区分。
- 交付门：`cd backend && make format && make lint && make test`、
  `uv run python scripts/generate_schema_comments.py --check`、
  `cd frontend && pnpm check && pnpm test` 及受影响的 Playwright 套件。
- 词汇：新增术语用 CONTEXT.md 流程（Knowledge Segment Summary）；摘要模型措辞是
  System Model（文本模型），不是 Provider Model。

## 范围与任务对应

| 功能 | 主要任务 | 验收任务 |
| --- | --- | --- |
| F01 查询向量缓存 | T1、T4 | T10 |
| F02 分段摘要索引 | T1、T5、T6、T7、T9 | T10、T11 |
| F03 知识库系统设置 | T1、T2、T3、T8 | T10 |

## 文件范围

现有文件（实施时重新核对行号，探索记录为 2026-08-31 基线）：

```text
backend/packages/knowledge/actweave_knowledge/
  contracts.py __init__.py module.py
  persistence/models.py persistence/tasks.py
  retrieval/service.py models/client.py
  ingestion/pipeline.py ingestion/reembed.py ingestion/progress.py
  segments/service.py documents/service.py bases/service.py
  storage/minio_store.py
backend/app/knowledge/ config.py composition.py model_port.py gateway.py worker.py
backend/app/gateway/deps.py backend/app/gateway/app.py
backend/app/gateway/routers/admin_operations.py admin_model_settings.py
backend/app/worker/app.py
backend/app/audit/models.py
backend/packages/harness/deerflow/config/app_config.py
backend/packages/harness/deerflow/persistence/
  full_schema.sql final_schema_contract.py final_schema_digest.py schema_comments.sql
backend/scripts/setup_postgres.py check_postgres.py generate_schema_comments.py
backend/scripts/run_replay_gateway.py
backend/tests/conftest.py backend/tests/replay_knowledge.py
backend/tests/_replay_fixture.py backend/tests/replay_worker_process.py
backend/tests/knowledge/（全部既有测试、eval_quality.py、eval_metrics.py、
  _generate_m10_eval_corpus.py、fixtures/m10_retrieval_cases.json）
frontend/src/core/knowledge/{types,api,hooks}.ts
frontend/src/core/admin-operations/types.ts
frontend/src/components/projects/knowledge/knowledge-base-detail.tsx
  knowledge-search-panel.tsx knowledge-segments-browser.tsx knowledge-documents-view.tsx
frontend/src/components/admin/operations/admin-operations-shell.tsx
frontend/src/core/i18n/locales/{en-US,zh-CN,types}.ts
frontend/tests/e2e/project-knowledge.spec.ts
frontend/tests/e2e-real-backend/knowledge-real-backend.spec.ts
config.example.yaml README.md Install.md CONTEXT.md backend/AGENTS.md frontend/AGENTS.md
docs/knowledge/RAG知识库设计文档.md
```

建议新增（出现实际调用方才落地，不先建空框架）：

```text
backend/packages/knowledge/actweave_knowledge/retrieval/query_cache.py
backend/packages/knowledge/actweave_knowledge/ingestion/summarize.py
backend/packages/harness/deerflow/persistence/knowledge_settings/model.py
backend/app/knowledge_settings/{__init__,service,bootstrap}.py
backend/app/gateway/routers/admin_knowledge_settings.py
backend/scripts/migrate_knowledge_config.py
backend/tests/knowledge/test_query_cache.py test_summaries.py
backend/tests/test_knowledge_settings_postgres.py
frontend/src/app/admin/settings/knowledge/page.tsx
frontend/src/components/admin/settings/admin-knowledge-settings-page.tsx
frontend/src/core/admin-settings/knowledge/{api,types,hooks,query-keys,index}.ts
frontend/tests/e2e/admin-knowledge-settings.spec.ts
frontend/tests/unit/core/admin-settings/knowledge-hooks.test.tsx
```

## 实施纪律

1. Task 是内部工作项；strict DTO/Schema 变更在分支内联调后一起交付，不发布中间不兼容态。
2. 每项按聚焦失败测试 → 最小实现 → 回归验证推进；权限、lease、版本、密钥回归不得随重构删除。
3. 不改 M9/M10 的模型所有权、原生分数、词法、预算与三分支契约；召回改动只按 §T7 的合并点扩展。
4. 现有数据库处置须单独确认；本计划不引入迁移框架，不补列、不 stamp。
5. 离线/replay 证据与真实模型质量分别报告；F02 未过质量门不得标记完成。

## 阶段 A：基线与一次定义

### T0：固定基线与交付前提

- [x] 记录基线 commit（`b94d8b34`）与工作树非知识域脏文件清单；确认 M10 交付状态与
  `tests/knowledge` 全量绿作为起点证据。
- [x] 确认真实质量门前提：SiliconFlow（或等价）文本 chat 模型可用性与预算（摘要生成 =
  语料段数次调用）、评测语料扩充的标注人力。没有不阻塞 T1–T10，但阻塞 T11 放行。
- [x] 确认操作者数据库处置意向仅作记录：M11 验证走临时隔离库 + 全新空库安装；目标库是否
  重建在 T11 前另行确认。
- [x] 复核 `.env` 是否有 `ACT_WEAVE_SECRET_KEY`（F03 加密依赖）与 `DATABASE_URL`；缺失记录为
  部署前提。

验收：以上四项有书面记录（追加到本文件 T0 落地记录），无未经授权的破坏性动作。

#### T0 落地记录（2026-08-31）

- 基线：规格基线 `b94d8b34`；实施起点 `3676c3b5`（操作者授权将工作树中与知识域无关的
  未提交改动整体提交为 `feat(runs): surface graph recursion and vision budget exhaustion
  failures…`，此后工作树干净）。操作者明确授权在 `main` 分支直接实施（不建功能分支）。
- 起点测试证据（实施起点 `3676c3b5` 重跑）：`tests/knowledge` core gate 723 collected /
  718 passed / 0 failed / 5 skipped——5 个 skip 全是 `test_storage.py` MinIO 集成例，导出
  `.env` 的 `ACT_WEAVE_KNOWLEDGE_MINIO_*` 后 17/17 通过、零跳过（本机 MinIO 可用）。
  **后续所有任务的门禁命令须先 `set -a && source ../.env && set +a` 以满足零跳过门。**
- 真实质量门前提：根 `.env` 具备 `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY`（SiliconFlow）
  与 `ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY`；文本 chat 模型与摘要生成预算在 T11 运行前
  由操作者最终确认。语料扩充由实施方（含标注）完成、操作者验收。
- 部署前提：`.env` 具备 `ACT_WEAVE_SECRET_KEY`、`DATABASE_URL`、`POSTGRES_ADMIN_URL`、
  `ACT_WEAVE_KNOWLEDGE_MINIO_*`。**本机 `config.yaml` 当前含非空 `knowledge` 块**——T3 墓碑
  落地后，本机部署重启前必须先跑 `scripts/migrate_knowledge_config.py` 再删块（T11 演练项）。
- 数据库处置：M11 全部验证走临时隔离库与全新空库；操作者目标库（`deerflow_knowledge`）
  是否随 M11 重建，T11 前另行确认，本计划不含 reset 授权。

### T1：Schema 与包契约一次定义

依赖 T0。所有列都要中文注释；strict DTO 未知字段拒绝。

**新表 1：`knowledge_segment_summaries`**（包表，`actweave_knowledge/persistence/models.py`，
归入 `KnowledgeOrmBase.metadata`）：

- [ ] `KnowledgeSegmentSummaryRow`：`id UUID PK`；`project_id/knowledge_base_id/
  knowledge_document_id UUID NOT NULL`；`knowledge_segment_id UUID NOT NULL`
  FK→`knowledge_segments.id` `ON DELETE CASCADE` + 唯一约束
  `uq_knowledge_segment_summaries_segment`；`document_version Integer NOT NULL`（CHECK ≥1）；
  `content Text NOT NULL`（CHECK `length(content) > 0`）；`source_content_digest String(64)
  NOT NULL`；`embedding Vector() NOT NULL` + CHECK `public.vector_dims(embedding) BETWEEN 1
  AND 16000`（命名沿 `ck_knowledge_segment_children_embedding` 风格）；`created_at
  timestamptz NOT NULL DEFAULT now()`。索引：`ix_knowledge_segment_summaries_scope
  (project_id, knowledge_base_id)`、`ix_knowledge_segment_summaries_document
  (knowledge_document_id)`。
- [ ] 注册进 `final_schema_contract.KNOWLEDGE_APP_TABLES`（现 L21-31 八表 → 九表）、
  `check_postgres.REQUIRED_TABLES`、`tests/knowledge/test_schema_repository.py::KNOWLEDGE_TABLES`。

**新表 2：`knowledge_system_settings`**（宿主表，新文件
`deerflow/persistence/knowledge_settings/model.py`，归入宿主 `Base.metadata`，自动进
`FINAL_APP_TABLES`）：

- [ ] `KnowledgeSystemSettingsRow`：`id SmallInteger PK` + CHECK `id = 1`（单行）；
  `revision Integer NOT NULL DEFAULT 1`；`enabled Boolean NOT NULL DEFAULT false`；
  `worker_concurrency SmallInteger NOT NULL DEFAULT 2`（CHECK 1..16）；
  `task_timeout_seconds Integer NOT NULL DEFAULT 900`（CHECK 30..7200）；
  `upload_max_bytes BigInteger NOT NULL DEFAULT 52428800`（CHECK 1..52428800）；
  `max_knowledge_bases_per_project Integer NOT NULL DEFAULT 20`（CHECK ≥1）；
  `max_documents_per_knowledge_base Integer NOT NULL DEFAULT 500`（CHECK ≥1）；
  `max_segments_per_document Integer NOT NULL DEFAULT 5000`（CHECK 1..5000）；
  `minio_endpoint String(512) NULL`、`minio_bucket String(255) NULL`、
  `minio_access_key String(512) NULL`、`minio_secure Boolean NOT NULL DEFAULT false`、
  `minio_secret_nonce LargeBinary NULL`、`minio_secret_ciphertext LargeBinary NULL`；
  `summary_model_name String(36) NULL`（System Model UUID 字符串，语义同 vision_bridge 的
  `ModelName`）；`query_cache_enabled Boolean NOT NULL DEFAULT true`、
  `query_cache_max_entries Integer NOT NULL DEFAULT 512`（CHECK 16..65536）、
  `query_cache_ttl_seconds Integer NOT NULL DEFAULT 300`（CHECK 5..86400）；
  `updated_at timestamptz NOT NULL DEFAULT now()`。
- [ ] CHECK `ck_knowledge_system_settings_secret_pair`：nonce 与 ciphertext 同空同非空，
  非空时 `octet_length(nonce)=12 AND octet_length(ciphertext)>=16`（沿
  `model_providers` L77-80 风格）；CHECK `ck_knowledge_system_settings_enabled_requires_minio`：
  `NOT enabled OR (endpoint/bucket/access_key/nonce/ciphertext 全非空)`。
  `__repr__` 排除密文列。

**既有表增量**：

- [ ] `knowledge_bases.summary_index_enabled Boolean NOT NULL server_default false`
  （models.py L38-78 区）。
- [ ] `knowledge_tasks`：`ck_knowledge_tasks_kind` 加 `summarize_document`（六种）；
  `ck_knowledge_tasks_target_version` 的"必须非空"集合扩为三种索引/摘要 kind；
  `uq_knowledge_tasks_open_indexing`（L490-496）的 partial WHERE 扩为
  `kind IN ('ingest_document','reembed_document','summarize_document')`；
  `ck_knowledge_tasks_reparse_settings` 不变（仍仅 ingest 可携带）。

**包契约（`contracts.py`）**：

- [ ] 字面量与常量：`KnowledgeIndexingTaskKind` 加 `"summarize_document"`；
  `KnowledgeTaskStage` 加 `"summarizing"`；新增
  `KNOWLEDGE_SUMMARY_PROMPT_VERSION = 1`、`KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS = 200`、
  `KNOWLEDGE_SUMMARY_MAX_CHARS = 1000`、`KNOWLEDGE_SUMMARY_MAX_TOKENS = 1024`、
  `KnowledgeMatchedVia = Literal["segment", "child", "summary"]`。
- [ ] `KnowledgeSettings` 加 `query_cache_enabled: bool = True`、
  `query_cache_max_entries: int = Field(default=512, ge=16, le=65536)`、
  `query_cache_ttl_seconds: int = Field(default=300, ge=5, le=86400)`。
- [ ] DTO：`KnowledgeSegmentSummaryView{content: str, created_at: datetime}`；
  `KnowledgeSegmentDetail` 加 `summary: KnowledgeSegmentSummaryView | None = None`；
  `KnowledgeBaseView` 加 `summary_index_enabled: bool`；`KnowledgeBaseUpdate` 加
  `summary_index_enabled: bool | None = None`；新增
  `KnowledgeSummaryBackfill{accepted_document_count: int, skipped_document_ids: tuple[UUID, ...]}`
  与 `KnowledgeBaseUpdateResult{base: KnowledgeBaseView, summary_backfill:
  KnowledgeSummaryBackfill | None = None}`（`update_knowledge_base` 返回类型改为它）；
  `KnowledgeRouteCounts` 加 `summary_candidates: int = 0`、
  `query_embedding_cache_hits: int = 0`、`query_embedding_cache_misses: int = 0`；
  `KnowledgeHitDiagnostics` 加 `matched_via: KnowledgeMatchedVia`。
- [ ] `KnowledgeModelPort`（L140-169）加两个方法：
  `async def resolve_summary_model(self, session: AsyncSession) -> str | None`（返回当前
  已配置且活跃的摘要模型引用，未配置返回 None，配置了但失效抛
  `KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE)`）与
  `async def generate_summary(self, *, model_ref: str, prompt: str) -> str`（不持 session；
  失败抛 `KNOWLEDGE_MODEL_UNAVAILABLE`（模型失效）或 `KNOWLEDGE_TASK_FAILED`（调用失败，
  安全消息））。`__init__.py` 导出全部新符号。

**五件套与测试**：

- [ ] `full_schema.sql` 两张建表 + bases/tasks 增量；重生成 `schema_comments.sql`（新列全部
  中文注释）并同步 `generate_schema_comments.py` 期望计数；更新
  `FINAL_SCHEMA_V1_CATALOG_SIGNATURE` 九项 count/digest 与 `SCHEMA_V1_CANONICAL_DIGEST`
  （临时空库安装后复读取值）。
- [ ] 测试：`test_schema_repository.py`（新表列奇偶自动覆盖 + IntegrityError 行为探针：
  单行 CHECK、每段唯一摘要、CASCADE 删段删摘要、enabled-requires-minio、summarize 开放任务
  与 ingest 互斥）；`test_package.py`（新 DTO/常量导出与 strict 拒绝）；
  `tests/test_schema_comments_contract.py` 回归。

验收：临时空库一次安装成功且签名复读一致；`make lint` 干净；不依赖 runtime 建表。

## 阶段 B：F03 设置面

### T2：设置行服务、播种与管理 API

依赖 T1。

- [ ] 新域 `backend/app/knowledge_settings/`：
  - `service.py`：`knowledge_minio_secret_recipient(endpoint: str) -> str`（
    `f"knowledge-system-settings:minio-secret-key:{endpoint}"`——换 endpoint 即换 recipient，
    旧密文不可移用，PUT 时改 endpoint 必须同时重交 secret，否则 422）；
    `async def read_knowledge_system_settings(session) -> KnowledgeSystemSettingsRow`；
    `async def load_knowledge_settings_from_db(session_factory, *, secret_key: SecretKey)
    -> KnowledgeSettings`（解密 → `KnowledgeMinioSettings`，行缺失按禁用处理并记录）；
    `async def read_active_summary_model(session) -> SummaryModelInfo | None`
    （`SummaryModelInfo{model_name: str, display_name: str}`，join
    `SystemModelConfigRow.status == "active"`；行内引用非空但模型失效返回 None 与失效标记，
    供 GET 展示、resolve 抛错）；
    `async def update_knowledge_system_settings(session_factory, *, actor, request, secret_key,
    audit_service, storage_probe) -> KnowledgeSystemSettingsRow`——单事务 `FOR UPDATE` 锁行、
    `expected_revision` CAS（不符 409 语义错误）、字段校验经 `KnowledgeSettings` 复验、
    `summary_model_name` 非空时按 `system_runtime_settings/service.py` L252-289 模式
    `FOR SHARE` 校验活跃 System Model、`enabled=true` 时以提交值（含保留密钥）构造
    `KnowledgeMinioSettings` 先跑 `storage_probe`（默认实现 =
    `MinioObjectStore(settings).require_unversioned_bucket()` 包 `asyncio.wait_for(…, 10)`，
    在事务外探测、探测通过后开写事务），失败 422 整体不落库；成功 `revision+1`、
    `updated_at`，审计 `append`。
  - `bootstrap.py`：`bootstrap_knowledge_system_settings(session_factory) -> None`，仿
    `system_runtime_settings/bootstrap.py` L116-188（`pg_advisory_xact_lock` + 幂等：有行校验、
    无行插 id=1 全默认禁用行）。
- [ ] `scripts/setup_postgres.py`：调用序（L524-533）加 `_bootstrap_knowledge_settings_schema
  (engine)`；`reset_postgres.py` 经共享路径自动获得。
- [ ] 审计：`app/audit/models.py` 加 `AuditAction.KNOWLEDGE_SETTINGS_UPDATED =
  "knowledge_settings.update"`，contract 沿 `SYSTEM_SETTING_UPDATED`（L542）模式
  （target `SYSTEM_SETTING`、actor system_admin、成功/拒绝/失败三 outcome），metadata 白名单
  模型为空对象（不带字段值）。
- [ ] 新路由 `backend/app/gateway/routers/admin_knowledge_settings.py`：
  `prefix="/api/admin/settings/knowledge"`、`route_class=AdminOperationsRoute`、管理员依赖仿
  `admin_model_settings.current_model_admin_context`（L272-288，非管理员 404 +
  `FinalSchemaProbe().require_ready`）。
  - `GET ""` → `AdminKnowledgeSettingsResponse`（strict）：全部非密字段 +
    `secret_key_configured: bool` + `summary_model: {model_name, display_name} | None` +
    `revision` + `request_id`；无 `minio_secret_key` 字段。
  - `PUT ""` ← `AdminKnowledgeSettingsUpdateRequest`（strict）：`expected_revision: int` +
    全部可写字段；`minio_secret_key: str | None = None`（None=保留旧值，空串 422）；请求 DTO
    `repr`/`model_dump` 不泄密（SecretStr 或显式排除）。响应同 GET 形状。409/422/404 映射与
    既有 admin 路由一致。
  - 在 `backend/app/gateway/app.py` L387-391 路由注册块挂载。
- [ ] 测试：新 `backend/tests/test_knowledge_settings_postgres.py`——bootstrap 幂等（两次调用
  一行不变）、setup 播种默认行、CAS 冲突、探测失败不落库（注入失败 `storage_probe`）、
  探测成功落库且 revision 递增、endpoint 变更未重交密钥 422、summary_model 校验（活跃通过/
  停用 422/非 UUID 422）、审计行存在且 metadata 无字段值、`enabled=true` 缺 MinIO 的 DB CHECK
  与服务校验、secret 不回显（GET 响应断言无字段 + DTO repr/model_dump 断言，仿
  `test_admin_api.py` L397-409）、非 system_admin 404。

验收：管理员能经 API 读写设置行；密钥全链路零泄漏；探测失败保持旧行原样。

### T3：启动装配切换、降级、墓碑与迁移

依赖 T2。

- [ ] `app/knowledge/config.py`：删除 `load_knowledge_settings(app_config)` YAML 路径；模块改为
  重导出 `load_knowledge_settings_from_db`。`app_config.py` 的 `YAML_CONFIG_TOMBSTONES`
  （L95）加 `"knowledge"`，并在 `reject_removed_legacy_config`（L338-369）为该键定制拒绝文案：
  指向 `scripts/migrate_knowledge_config.py` 与管理页"系统设置 → 知识库配置"。
  `config.example.yaml` 删除 knowledge 块、原位留迁移注释。
- [ ] `app/knowledge/composition.py`：`create_knowledge_module_from_app_config` 改为
  `async def create_knowledge_module_from_database(*, app_config) -> tuple[KnowledgeModule |
  None, KnowledgeStartupState]`，`KnowledgeStartupState = Literal["ready", "disabled",
  "storage_failed"]`：engine 就绪后读设置行 → 禁用返回 `(None, "disabled")` → 组合模块（
  `RegistryKnowledgeModelPort` 构造加 `model_runtime`/`summary_model_reader`，见 T5）→
  `module.health()` 存储校验失败：记日志（不含端点/凭据）、`await module.aclose()`、返回
  `(None, "storage_failed")`。`KnowledgeStorageNotReady` 异常类保留给留存 fail-closed 语义。
- [ ] Gateway：`gateway/deps.py` L358-369 改调新工厂，`app.state.knowledge_module` +
  `app.state.knowledge_startup_state` 两个状态；`storage_failed` 不再中止 lifespan。
  Worker：`worker/app.py` L182-191 同改；模块为 None（含 storage_failed）时只跑主循环。
  `create_knowledge_worker_resources_from_app_config`（composition.py L66-81）同步改为
  DB 来源：留存 purger 与功能模块继续独立组装——设置行禁用或缺 MinIO 时 purger 保持现有
  "元数据可清、存储证据不全则 fail-closed" 语义不变。
- [ ] readiness：`admin_operations.py` 的 `OperationsReadinessResponse`（L77-103）加
  `knowledge: Literal["ready", "disabled", "unavailable"]`（Gateway 进程自身状态：
  ready/disabled/storage_failed→unavailable），`overview_response` 映射同步；该字段不携带
  端点/凭据/错误详情。前端 `core/admin-operations/types.ts` strict schema 同步。
- [ ] 迁移脚本 `backend/scripts/migrate_knowledge_config.py`：**不经 `get_app_config()`**
  （墓碑会拒绝）——直接 `yaml.safe_load(ACT_WEAVE_CONFIG_PATH 或仓库根 config.yaml)` +
  `resolve_env_variables`（从 `app_config.py` 导入）取 knowledge 块，经 `KnowledgeSettings`
  校验，`SecretKey.from_environment()` 加密，engine 由根 `.env` 的 `DATABASE_URL` 直连，
  upsert id=1（幂等，重跑覆盖同值、revision 递增），stdout 报告字段清单（密钥打码）。
  文档化操作顺序：停服 → 跑迁移（YAML 块仍在）→ 删块 → 启动新版本。
- [ ] replay/真实后端启动器改为播种设置行：`scripts/run_replay_gateway.py`、
  `tests/_replay_fixture.py`、`tests/replay_worker_process.py` 在建库后写入 enabled 设置行
  （MinIO 临时容器参数 + 加密密钥），不再依赖 YAML knowledge 块。
- [ ] 测试：`tests/knowledge/test_host_config.py` 重写为 DB 装载（行缺失=禁用、解密往返、
  损坏密文明确失败）；app_config 墓碑测试（knowledge 块 → `LEGACY_CONFIG_REMOVED` 且文案
  含迁移指引）；composition 三态（disabled/ready/storage_failed，后者断言模块缺席 + 知识路由
  404 `KNOWLEDGE_DISABLED` + readiness `unavailable`）；迁移脚本幂等与密钥打码（
  `test_knowledge_settings_postgres.py` 内）；worker 降级路径（模块 None 时主循环正常）。

验收：全新空库 + 管理页配置即可启用知识模块；YAML 带 knowledge 块的启动被明确拒绝；
存储不可达时 Gateway/Worker 照常服务其他域、readiness 可见 `unavailable`。

## 阶段 C：F01 查询向量缓存

### T4：进程内 LRU+TTL 缓存

依赖 T1（settings 三字段）；与 T2/T3 可并行（包侧只消费 `KnowledgeSettings`）。

- [ ] 新 `retrieval/query_cache.py`：

  ```python
  class KnowledgeQueryEmbeddingCache:
      def __init__(self, *, enabled: bool, max_entries: int, ttl_seconds: float,
                   clock: Callable[[], float] = time.monotonic) -> None: ...
      def get(self, model_id: UUID, query: str) -> tuple[float, ...] | None: ...
      def put(self, model_id: UUID, query: str, vector: Sequence[float]) -> None: ...
  ```

  键 `(model_id, sha256(query.encode("utf-8")).digest())`；`OrderedDict` LRU（get 触达移尾、
  put 超容量弹头）；TTL 读时惰性过期；`enabled=False` 时 `get` 恒 None、`put` 恒 no-op；
  纯同步无锁（asyncio 单线程内原子）；值转只读 tuple。不做 single-flight。
- [ ] `module.py`：`KnowledgeModule.__init__` 按 settings 构造缓存实例，传入
  `KnowledgeSearchService`（L137-140 构造点加 `query_cache=`）。
- [ ] `retrieval/service.py` `search()` L694-716：分组嵌入前先 `cache.get(model_id,
  validated.query)`——命中计 hit、跳过 `_dispatch_guard` 与 `self._client.embed`；未命中照旧
  （guard 在前）后 `cache.put`；本次搜索的 hit/miss 计数进 `KnowledgeRouteCounts`
  两个新字段；`timings.query_embedding_ms` 语义不变（命中≈0）。搜索内既有 `query_vectors`
  字典保留（单请求内复用）。
- [ ] `backend/AGENTS.md` 知识节授权措辞更新为"每次**面向 Provider 的**查询嵌入（缓存未命中）
  前回验授权；缓存命中不产生 Provider 调用，召回事务与终审回验不变"。
- [ ] 测试：新 `tests/knowledge/test_query_cache.py`（纯缓存：命中/TTL 过期/LRU 淘汰/模型隔离/
  禁用恒未命中/超长值容量边界）；`test_retrieval.py` 增：同查询第二次搜索 Provider embed
  调用数为零（MockTransport 计数）且两次 hits 逐字节一致、缓存全热下撤权成员仍在召回事务
  边界被拒（既有撤权用例扩一次预热）、rebuild 换模型后旧条目天然失效（新 model_id 未命中）、
  两个并发同查询 `asyncio.gather` 无死锁双成功、诊断计数正确、HTTP debug round-trip 带新计数。

验收：缓存是纯性能层——开/关/冷/热的检索结果与授权行为不可区分，Provider 调用次数可证减少。

## 阶段 D：F02 分段摘要索引（后端）

### T5：摘要生成任务与模型接入

依赖 T1、T2（`resolve_summary_model` 读设置行）。

- [ ] 宿主端口实现（`app/knowledge/model_port.py`）：`RegistryKnowledgeModelPort.__init__` 加
  `model_runtime: ModelRuntime | None = None`；`from_environment()` 改由 composition 传入
  `ModelRuntime(app_config=get_app_config())`。
  - `resolve_summary_model(session)`：读 `knowledge_system_settings.summary_model_name`
    （每次调用重读——即时生效语义）；空 → None；非空 → `SystemModelConfigRow` 活跃校验
    （`FOR SHARE`），失效抛 `KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE)`。
  - `generate_summary(*, model_ref, prompt)`：`await self._model_runtime.ainvoke([HumanMessage
    (prompt)], profile=ModelRuntimeProfile.PRIVATE_ONESHOT, model_name=model_ref,
    model_overrides={"max_tokens": KNOWLEDGE_SUMMARY_MAX_TOKENS},
    deadline_monotonic=time.monotonic() + 120)`（参照 `inspect_image_tool.py` L530-536）；
    返回 `message.content` 文本；Provider 异常折叠为
    `KnowledgeError(KNOWLEDGE_TASK_FAILED, "摘要生成失败")`（不透传 Provider 载荷）。
- [ ] 新 `ingestion/summarize.py`：
  - 模块级 `KNOWLEDGE_SUMMARY_PROMPT_V1`（固定中文指令模板：以源段落语言输出 ≤200 字摘要、
    保留关键实体/数值/结论、不添加评论；`{content}` 单占位）。
  - `class KnowledgeSummarizeHandler`（构造签名仿 `KnowledgeReembedHandler`：
    `session_factory, model_port, model_client, progress_factory`）：
    1. `_begin_processing`（单事务）：`lock_indexing_claim` + 文档行锁；校验
       `document.status == "ready"`、`document.version == claim.target_version`（隐含
       `published_version == version`）、base `active` 且 `summary_index_enabled`；
       `model_ref = await model_port.resolve_summary_model(session)`，None →
       `KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE)`；装载该版本全部 enabled+disabled 段
       （id, content）与现存摘要（segment_id, source_content_digest）；目标 = 字符数 ≥
       `KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS` 且（无摘要或 digest ≠ 当前内容 SHA-256）的段；
       取 `embedding_material(base.embedding_model_id)`。零目标 → 直接发布空操作成功。
    2. 事务外 stage `summarizing`（`total_units = len(targets)`）：每次 LLM 调用前跑 guard
       （`ensure_claim_alive` + Project active 复核，短事务）→ `generate_summary` → 超
       `KNOWLEDGE_SUMMARY_MAX_CHARS` 硬截断 → 逐条 `completed_units+1`；stage `embedding`：
       `model_client.embed(material, summary_texts, batch_guard=同一 guard,
       on_batch_verified=…)`。
    3. `_publish`（单事务）：复验 claim、`document.version == target` 且 ready、库开关仍开、
       `base.embedding_model_id` 未变（任一不符 no-op 放弃，沿 reembed L189-198）；**逐段复核
       当前内容 digest**——仍匹配快照的删旧插新（`document_version = document.version`），
       期间被编辑的段跳过不写；若有跳过且开关仍开，settle 成功后同事务补入队一个新
       `summarize_document`（此时旧任务已 succeeded，不违反开放唯一）；
       `settle_task_row_success`。
  - stage 流转 `queued → summarizing → embedding → publishing → done`，经
    `KnowledgeTaskProgressReporter`（`ingestion/progress.py`）短事务写入。
- [ ] `persistence/tasks.py`：新增 `VERSIONED_TASK_KINDS = ("ingest_document",
  "reembed_document", "summarize_document")`；`INDEXING_TASK_KINDS` 保持二元——
  `settle_task_failure` 耗尽只对 INDEXING 两种 `_mark_indexed_document_failed`，
  summarize 耗尽只任务 failed、文档保持 ready；`recover_expired_tasks` 对新 kind 走同一恢复。
- [ ] `module.py` `run_worker` handlers（L686-715）注册 `"summarize_document"`。
- [ ] 测试：新 `tests/knowledge/test_summaries.py`（鸭子型 port 记录 prompt/model_ref +
  真实 `KnowledgeModelClient` + MockTransport 嵌入）：完整生成发布（行字段全断言含 digest/
  version/embedding）；短段跳过与零目标成功；digest 一致跳过、不一致重生成；模型未配置
  `KNOWLEDGE_MODEL_UNAVAILABLE` 失败且文档 ready 摘要缺席；停用模型同前；LLM 中途失败保留
  既有摘要行、attempt 重试从零；迟到发布（版本翻动）no-op；发布期段被编辑 → 跳过 + 补任务；
  失租停止未派发批次；进度 stage/计数矩阵；截断与 prompt v1 快照（锁模板文字）。

验收：单文档摘要全生命周期确定可复现；一切失败都不损伤文档 ready 与既有行。

### T6：生命周期触发、准入与重处理交互

依赖 T5。

- [ ] `ingestion/pipeline.py` `_publish`（L273-374）：`settle_task_row_success` 之后、同事务内——
  base `summary_index_enabled` 且 `await model_port.resolve_summary_model(session)` 非 None
  且存在 ≥200 字符段 → `session.add(KnowledgeTaskRow(kind="summarize_document",
  project_id=…, resource_id=document.id, target_version=document.version))`（含 reparse 路径；
  handler 构造需新增 model_port 依赖）。
- [ ] `segments/service.py`：`update_segment` 写事务（L180-219）内删除该段摘要行；
  `update_segment`/`create_segment` 事务内若库开关开 + 模型可解析 + 无开放 VERSIONED 任务 →
  入队刷新 summarize（有开放任务则跳过——T5 发布端 digest 复核 + 补任务闭环兜底）。
  `delete_segment` 靠 FK CASCADE，无需改动（用测试锁行为）。
- [ ] `bases/service.py`：`update_knowledge_base`（L248-338）接受 `summary_index_enabled`；
  返回类型改 `KnowledgeBaseUpdateResult`。拨 ON：同事务 Base 锁 + 全文档 UUID 序锁（沿
  rebuild L370-393 模式但**跳过不拒绝**）——`published_version` 非空且 ready 且无开放
  VERSIONED 任务的入队（accepted），其余计 skipped；拨 ON 前
  `resolve_summary_model` 为 None → `KNOWLEDGE_INVALID_REQUEST`（422，文案指向管理页）。
  拨 OFF：仅写列。`_view` 投影补 `summary_index_enabled`。
- [ ] `ingestion/reembed.py`：`_PreparedReembed` 加 `summary_entries: tuple[tuple[UUID, str],
  ...]`（`_begin_processing` 按 published_version 段集合装载摘要 id+content）；handler 对
  摘要文本走同一新模型嵌入（合并进现有批量，`total_units` 相应累加）；`_publish`（L170-237）
  同事务 `update(knowledge_segment_summaries)` 写新向量并翻 `document_version`——摘要文本、
  digest、created_at 不动，零 LLM 调用。
- [ ] `documents/service.py`：reparse/上传/删除准入的开放任务检查（L379-393 等）与
  `retry_document` 继承查询（L755-766）从 `INDEXING_TASK_KINDS` 切到 `VERSIONED_TASK_KINDS`；
  `retry_document` 增加分支：文档 ready 且最近 VERSIONED 任务为 failed `summarize_document`
  → 仅重新入队 summarize（不动文档 status/version/计数）；`indexing_task_progress` 投影
  （kind 字面量已扩）确认覆盖新 kind 并保留"绑定当前代次"语义。
- [ ] 测试：`test_ingestion.py`（发布入队矩阵：开关开+模型配 → 入队；关/未配/全短段 → 不入队；
  reparse 路径同断言）；`test_governance.py`（编辑同事务删摘要 + 入队；开放任务时跳过入队；
  CASCADE 删除）；`test_bases.py`（拨 ON 回填 accepted/skipped 矩阵：ready 入队、开放任务/
  未发布/非 ready 跳过；模型未配 422；拨 OFF 行保留；HTTP round-trip 新响应形状）；
  `test_reembedding.py`（摘要文本逐字节保留 + 向量翻新 + document_version 翻转 + LLM 零调用
  ——鸭子 port 断言 `generate_summary` 未被调）；`test_tasks.py`（summarize 耗尽文档保持
  ready、开放 summarize 阻止 reparse/rebuild 准入、过期恢复、retry 重入队路径）。

验收：五个触发点（ingest/reparse 发布、拨 ON、段编辑、retry）与三个重处理交互（rebuild/
reparse/删除级联）全部行为确定；任何路径都不出现"新参数解释旧行"或摘要静默丢失。

### T7：召回集成与投影

依赖 T5/T6；与 T4 在 `service.py` 有共同修改面，按 T4 → T7 顺序合入。

- [ ] `retrieval/service.py` 新 `_summary_candidates(...)`（构造仿 `_general_candidates`
  L1790-1858）：`KnowledgeSegmentSummaryRow` join segments/documents/bases；过滤 =
  `_current_scope_filters` 全套 + `KnowledgeBaseRow.summary_index_enabled.is_(True)`；
  `vector_score = 1 - summary.embedding.cosine_distance(qv)`；每库 `row_number() ≤ C` 封顶
  （稳定序同两路：分数→文档→段位置→段 UUID）。
- [ ] 语义池合并点：general/parent_child/summary 三来源按段 id 取 max 成段语义原生分，
  记录 argmax 来源为 `matched_via`（`segment|child|summary`；lexical 新增项回填 cosine 时
  parent_child 取子块 max → `child`，general → `segment`，summary 贡献最高 → `summary`）；
  合并后再截每库 C（既有合并语义扩一个来源，RRF/三分支/阈值语义零改动——阈值作用于
  该 max 原生分）。`_Candidate`/`_Ranked` 携带 matched_via 直至 `KnowledgeHitDiagnostics`。
- [ ] 诊断：`counts.summary_candidates`（各组封顶后摘要候选和）；`search()` L900-932 组装
  同步。终审 `_reviewed_hits` 不需复核摘要行（hit 本体是段，内容 digest 复核已覆盖）。
- [ ] `segments/service.py` `get_segment_detail`：装载该段摘要行 → `summary` 字段。
- [ ] `gateway.py`：`KnowledgeModelOptionsResponse`（L297-309）加 `summary_model:
  KnowledgeSummaryModelResponse | None`（`{model_name: str, display_name: str}`，数据来自
  `read_active_summary_model`）；`KnowledgeBaseUpdateRequest`（L321-334）加
  `summary_index_enabled: bool | None`，PATCH 响应加 `summary_backfill`（strict 子模型）；
  base item 投影加 `summary_index_enabled`；`KnowledgeSegmentDetailResponse`（L748-759）加
  `summary`；`_search_diagnostics_response`（L1806-1852）投影三个新计数与 `matched_via`。
- [ ] `app/knowledge/run_tool.py`：ToolMessage 载荷与 citations **零变化**（摘要不进正文）——
  用既有断言锁形状。
- [ ] 测试：`test_search_ranking.py`/`test_retrieval.py` 增——摘要向量翻盘（原文无 marker、
  摘要含 marker 的段经 summary 路召回且 `matched_via="summary"`）；三来源 max 归因矩阵；
  开关关库/段禁用/文档非 ready/stale version 的摘要一律不出现在候选；阈值作用于回卷后
  max 分；每库 C 在合并后生效（摘要多的库不挤占预算外名额）；RRF 与三分支基线用例全量
  不回归；citations/passage 仍为段真实内容（专项断言摘要文本不在任何 citation/ToolMessage
  中出现）；`test_search_details.py` 详情带摘要与无摘要两态；HTTP round-trip（model-options
  的 summary_model 两态、PATCH backfill、debug 新字段）。

验收：F02 召回契约与 M10 基线完全兼容——关闭开关时全部既有测试原样通过；开启时新增来源
可归因、可预算、可排除。

## 阶段 E：前端

### T8：管理端"知识库配置"页

依赖 T2/T3（API 契约冻结）。

- [ ] `core/admin-settings/knowledge/`：`types.ts`（GET/PUT strict Zod：全部字段 +
  `secret_key_configured` + `summary_model` nullable + `expected_revision`；秘密字段仅在
  PUT 输入且可选）、`api.ts`（`fetchAdminKnowledgeSettings` / `replaceAdminKnowledgeSettings`
  仿 `admin-settings/system/api.ts` L134-233）、`hooks.ts`（query + mutation，onSuccess 失效
  自身 root）、`query-keys.ts`。
- [ ] 页面：`app/admin/settings/knowledge/page.tsx`（thin route）+
  `components/admin/settings/admin-knowledge-settings-page.tsx`——组成仿
  `admin-system-settings-page.tsx` 的 `FieldShell/BooleanField/NumberField/ModelField` 行式
  模板与 `EditableSection` 壳（单 section）：功能开关、存储表单（endpoint/bucket/access_key/
  secure/secret——secret 输入 `type=password`、留空=保留、占位"已配置"）、四项配额、
  摘要模型下拉（`useModels()` 全量活跃 System Model，含"未配置"空项）、缓存三参数；顶部
  常驻重启提示 Banner（文案区分摘要模型即时生效）；保存错误经 `safeActionError` 模式映射
  409/422（探测失败展示服务端安全文案）；`system_admin` 双门（layout 已有 + 组件 `useAuth`
  防御，仿 L2997-3001）。字段文案用组件内 `FIELD_COPY` 双语字典（house 风格），页头/反馈
  进 locales `adminKnowledgeSettings.*`。
- [ ] 导航：`admin-operations-shell.tsx` governance 组（L184-204）加
  `/admin/settings/knowledge` 项 + 面包屑匹配（L391-408）+ 导航 label i18n
  （`adminOperations.navigation.knowledgeSettings`）。运维页 readiness 展示加 knowledge
  组件状态行（`core/admin-operations/types.ts` 已在 T3 同步 schema）。
- [ ] i18n：`en-US.ts`/`zh-CN.ts`/`types.ts` 三处同步新增 `adminKnowledgeSettings` 节与导航
  key（注意与用户未提交改动共存，只追加）。
- [ ] 测试：`tests/unit/core/admin-settings/knowledge-hooks.test.tsx`（mock api 的失效行为）；
  新 `tests/e2e/admin-knowledge-settings.spec.ts`（mock：`/api/v1/auth/me` system_admin、
  GET/PUT 设置、`/api/models`）——渲染与草稿脏检查、保存成功回写 revision、409 冲突文案、
  422 探测失败文案持续可见、secret 写后占位且响应无泄漏、非管理员不可见入口、重启 Banner。

验收：管理员在 UI 完成从禁用到启用的全配置；错误可操作；密钥零回显。

### T9：知识域 UI 增量

依赖 T6/T7（HTTP 契约冻结）；与 T8 可并行。

- [ ] `core/knowledge/types.ts`：`knowledgeBaseItemSchema` + `UpdateKnowledgeBaseInput` 加
  `summary_index_enabled`；base update 响应加 `summary_backfill` nullable strict 子对象；
  `knowledgeModelOptionsResponseSchema`（L202-212）加 `summary_model` nullable（`api.ts`
  L627-630 的手工重组同步透传）；`knowledgeSegmentDetailResponseSchema` 加 `summary`
  nullable `{content, created_at}`；task kind/stage 枚举加 `summarize_document`/`summarizing`；
  诊断 schema 加 `summary_candidates`、两个缓存计数、`hit_diagnostics[].matched_via`。
- [ ] `knowledge-base-detail.tsx` 设置面板：retrieval 区（L449-473 后）加"摘要索引"开关卡片
  ——`modelOptions.summary_model` 为 null 时禁用 + 提示文案（指向管理员配置）；提交把
  `summary_index_enabled` 并入 PATCH（L320-343 的 input）；mutation 成功后若响应带
  `summary_backfill` 以 status 条展示 accepted/skipped（testid
  `knowledge-summary-backfill-outcome`）。
- [ ] 段详情：`SearchHitDetailDialog`（`knowledge-search-panel.tsx` L855-869 区）与
  `knowledge-segments-browser.tsx` 详情视图渲染摘要块（标注"系统生成摘要" + created_at，
  与正文视觉区分，无摘要不渲染）。
- [ ] 进度：`knowledge-documents-view.tsx` `TaskProgressLine`（L180-218）依赖的 i18n
  `knowledge.documents.progress.kinds.summarize_document` 与 `stages.summarizing`
  三文件补齐。
- [ ] 诊断面板（`knowledge-search-panel.tsx` 折叠区）：新计数三项 + 逐 hit `matched_via`
  徽标（Segment/Child/Summary）。
- [ ] mock e2e `project-knowledge.spec.ts`：`mockKnowledgeRoutes` 的 model-options 分支
  （L401-422）加 `summary_model` 可配开关；baseView 补 `summary_index_enabled`；PATCH 分支
  返回 `summary_backfill`；segment detail mock 带摘要；进度 mock 带 summarize 任务。用例：
  未配置时开关禁用+提示；配置后拨 ON 显示回填计数；详情摘要展示；进度行文案；诊断新字段
  与 matched_via 徽标。
- [ ] 单测：`tests/unit/core/knowledge/` 相应 schema/纯函数用例（strict 拒绝旧形状、缺省
  nullable 兼容）。

验收：`pnpm check` 干净；mock e2e 全绿；所有 strict schema 与后端 DTO 逐字段对齐。

## 阶段 F：验证与交付

### T10：确定性全链路与安全门

依赖 T1–T9。

- [ ] `tests/replay_knowledge.py`：`_build_provider_app` 加 `POST /v1/chat/completions`
  （OpenAI 形状；锁内 `chat_calls` 计数 + `chat_failures` 故障注入，`ProviderFaults` 同步）；
  确定性输出规则：`f"{DOC_RERANK_MARKER}摘要{sha256(prompt)[:8]}"`——摘要必含 marker，
  使"原文无 marker、摘要可召回"在 replay 向量合同下可证明。System Model 种子：仿
  `tests/support/system_model_seed.py` 在 `seed_replay_model_registry` 旁新增文本模型行
  （openai 适配器指向 replay base_url），并在 replay 启动器把
  `knowledge_system_settings.summary_model_name` 指向它。
- [ ] `knowledge-real-backend.spec.ts` 扩展（临时 PG + MinIO + replay Worker）：库拨 ON →
  上传发布 → summarize 任务进度可见 → 摘要在段详情展示 → marker 查询经 summary 命中
  （debug `matched_via="summary"`）→ Re-embed 后摘要文本不变仍可召回（chat_calls 零增长）
  → Reparse 后摘要重建（新任务、旧摘要消失）。管理设置页 real-backend 至少覆盖 GET 渲染
  与 PUT 探测失败一例（错误 MinIO endpoint）。
- [ ] 后端全量门：`make test`（core gate 零跳过）+ `make lint` +
  `generate_schema_comments.py --check`；`tests/knowledge` 单独跑一遍记录数量。前端：
  `pnpm check`、`pnpm test`、`project-knowledge.spec.ts`、`admin-knowledge-settings.spec.ts`、
  real-backend 套件、`pnpm build:production`。
- [ ] 安全门专项汇总（跨任务断言已存在，此处逐项确认并记录）：secret 五路径零泄漏（响应/
  日志/审计/repr/诊断）；摘要文本不进 citation/ToolMessage/普通日志；readiness 不含端点；
  撤权在缓存全热与摘要在场时行为不变。
- [ ] 失败先归属再处置：与既有测试冲突时先在基线 worktree 复现定位，不得顺手改无关域。

验收：全部确定性门绿并留存执行记录（数量/失败/跳过逐项）；mock 与 replay 证据不冒充真实质量。

### T11：真实质量门、文档与交付确认

依赖 T10。

- [ ] 语料扩充：`_generate_m10_eval_corpus.py` 新增 `question_style` 类目（答案在长段落、
  问句与正文表述有明显鸿沟；dev ≥10、holdout ≥10，判定单位与三级相关性沿 M10）；
  `test_m10_eval_corpus.py::REQUIRED_CATEGORIES` 与分层断言同步；fixture `gates` 加 M11 键：
  `question_recall_candidate_uplift_pp ≥ 5`、`question_recall_at_10_uplift_pp ≥ 5`、
  `overall_ndcg_regression ≤ 0.02`、`no_answer_false_recall_not_worse`、既有类目不低于
  M10 放行水位。
- [ ] `eval_quality.py`：变体轴从 `("semantic", "hybrid")` 扩为 (mode × summary on/off)——
  语料摄取后为 opt-in 库真实生成摘要（真实 chat 模型，记录调用数与费用），off 趟拨
  OFF（行保留、召回排除，零重生成）；`summarize`/`evaluate_gates`/`write_report` 按新轴
  聚合；报告写 `docs/knowledge/m11-quality-eval-report.{json,md}`。运行入口仿
  `test_m10_quality_eval.py`（`ACT_WEAVE_KNOWLEDGE_QUALITY_EVAL=1` + provider_integration）。
- [ ] 性能与运行证据：非 Provider P95 对比 M10 基线（>20% 恶化须优化或记录产品复审）；
  缓存命中率（Agent 复用场景两连搜的诊断计数）；单文档摘要 LLM 调用数与 skipped 计数
  入报告。预算/语料不可用 → 按 M10 协议记录 `blocked_pending_operator_input`，F02 不得
  标记完成。
- [ ] 文档同批更新：README（知识配置改为管理页 + 迁移指引）、Install（迁移顺序：停服 →
  迁移脚本 → 删 YAML 块 → 启动）、`config.example.yaml`（已在 T3）、`backend/AGENTS.md`
  （F03 装配、新表、授权措辞已在 T4）、`frontend/AGENTS.md`（新管理页与知识 UI 增量）、
  `CONTEXT.md`（Knowledge Segment Summary 词条 + 摘要模型是 System Model 的措辞）、
  `docs/knowledge/RAG知识库设计文档.md`（三功能行为）。
- [ ] 交付确认：全新空库安装 + `make check-db` 只读证据；操作者目标库处置单独确认（本计划
  不是 reset 授权）；存量部署迁移路径演练记录（迁移脚本在含 knowledge 块的 YAML 上跑通）。
- [ ] M11 总状态最后才从"计划中"更新为"已完成"。

验收：问题式查询类目双召回指标 +5pp、全量 nDCG 回退 ≤0.02、无答案不恶化、既有类目不回退；
文档与部署证据齐备。

## 依赖与并行安排

- T0 → T1 是共同前置；T1 冻结全部 Schema/DTO/端口签名。
- F03 链：T2 → T3；F01（T4）只依赖 T1，可与 T2/T3 并行。
- F02 链：T5（依赖 T2 的设置行读取）→ T6 → T7；T7 与 T4 共改 `retrieval/service.py`，
  按 T4 先合。
- 前端 T8（依赖 T2/T3）与 T9（依赖 T6/T7）可并行推进。
- T10 → T11 为交付门；前后端与 Schema 同步交付，不发布"暂时只有后端"的中间态。

## 完成清单

- [ ] F01–F03 逐项有实现与验收证据；M10 既有能力无重复建设或退化（基线契约测试全量不回归）。
- [ ] 没有摘要文本进入引用/工具正文、密钥泄漏、跨项目披露、迟到任务写入旧代次。
- [ ] 缓存开/关/冷/热结果与授权行为不可区分，仅 Provider 调用次数不同。
- [ ] 设置行是知识配置唯一来源；YAML 墓碑生效；迁移脚本幂等；降级启动可观测。
- [ ] 所有确定性门通过，真实质量/性能门有有效结果或明确受阻记录。
- [ ] Schema 五件套、DTO、装配、retention、文档一致；部署确认单独完成。
