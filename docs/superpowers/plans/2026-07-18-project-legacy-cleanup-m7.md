# M7 Final Legacy Cleanup and Pre-release Baseline Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 DeerFlow 所有 pre-release legacy runtime/source/API/UI/config/migration 路径，把 M1–M6 最终产品收敛为一个 PostgreSQL baseline 和唯一 project-first SaaS 运行面。

**Architecture:** 先建立不依赖 cutover marker 的 final-schema contract，再逐域删除 shared-assets、private-work、Automation、channel 和 frontend 兼容面；所有 final project 能力继续使用不可变 `ProjectContext`、PostgreSQL authority、独立 Worker/Scheduler 和 durable SSE。最后删除旧 Alembic 链并生成 `0001_project_saas_baseline`，使 setup 只接受空库、restore 只接受 M7 archive，固定 M1–M7 gate 证明旧 surface 不存在且 M1–M6 能力不退化。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy async、Alembic、PostgreSQL 17、asyncpg、pytest/pytest-asyncio、cryptography AES-GCM、Next.js 16、React 19、TypeScript 5.8、TanStack Query 5、Zod、Rstest、Playwright、pnpm 10.26.2。

## Global Constraints

- 项目未上线、无生产用户、无必须保留的生产数据库；不支持从旧 `0001`–`0015` 开发 revision 原地升级。
- 程序不得自动 drop、truncate、stamp 或删除任何旧本地数据库、`.deer-flow` 目录、备份或自定义文件；旧库只返回 `M7_RECREATE_REQUIRED`。
- 最终 Alembic head 固定为 `0001_project_saas_baseline`；旧 migration/cutover table、staged-only column 和 revision file 全部删除。
- PostgreSQL 继续是 account/project/private-work/asset/Automation/job/stream/quota/audit/recovery 的唯一在线 authority；不得增加 SQLite、filesystem、memory、Redis、Kafka 或双写 fallback。
- Gateway 只做认证、作用域解析、事务 admission、查询、durable SSE 和显式 project-scoped input-polish 辅助模型调用；只有 Worker 执行 Agent graph，只有 Scheduler 计算到期 Automation。
- 所有私有读写必须同时固定 `project_id + owner_user_id`；客户端不能提供可信 project、owner、membership、role、capability、asset snapshot、credential grant、job、lease 或 `non_interactive`。
- 被删除 backend URL 返回普通 `404`；被删除 frontend URL进入 not-found；禁止保留 redirect、`410`、`*_CUTOVER` 或迁移提示页。
- `config.yaml` 只从显式 `DEER_FLOW_CONFIG_PATH` 或 repo-root canonical path 读取；精确 tombstone validator 拒绝 `agents_api`、`run_events`、`stream_bridge`、`extensions` 和旧 alias。
- 内置 Agent/Skill/MCP 只由 versioned bootstrap catalog 写入 PostgreSQL；Gateway/Worker/Scheduler 运行时不得扫描 repo、user 或 extensions config 路径。
- M6 backup/restore/journal/drill authority、secret separation、inode ownership、fsync 和 new-database-only 规则全部保留；只接受 M7 baseline archive。
- 真实 PostgreSQL 测试只创建随机 `deerflow_test_*`/`deerflow_restore_*` 数据库；缺 `POSTGRES_TEST_URL` 必须在 pytest 前失败，release gate 保持 0 skip。
- Backend/Frontend 严格 TDD；每个任务先运行 RED，再写最小实现，运行 affected gate，更新相关 `AGENTS.md`，独立提交后才进入下一任务。
- 每个 Task 1–10 的独立提交都要用 `superpowers:requesting-code-review` 审查 `HEAD^..HEAD`，Critical/Important 必须在下一任务前修复并重跑 affected gate；Task 11 再审查完整分支。
- 删除 production fake 后，测试替身只能放在 `backend/tests/support/` 或 frontend test fixture；不得为测试保留 production memory/fallback backend。
- M7 完成只把总体进度更新为 7/8（87.5%）；M8 未完成时不得宣称完整多用户 SaaS 可发布。
- 开始执行前使用 `superpowers:using-git-worktrees` 建立 `codex/m7-legacy-cleanup` 隔离工作区，并以当前 `dev` HEAD 为基线。

---

## Execution status (2026-07-19)

Tasks 1–11 are implemented, repaired, and independently reviewed at 0 Critical / 0 Important /
0 Minor. Their exact implementation/review ranges are recorded in
[`2026-07-18-project-legacy-cleanup-m7-design.md`](../specs/2026-07-18-project-legacy-cleanup-m7-design.md#12-实施切片与已审查证据).
Task 11 closed the full branch at `e39aff39`; the final independent review reported 0 Critical,
0 Important, and 0 Minor findings. M7 is `Completed` and the master ledger is 7/8 (87.5%). M8
remains pending, so complete release readiness is not claimed.

## File Structure

### Final schema and setup

- `backend/app/final_schema.py` — 单一 final revision/required-relation probe；不包含 domain HTTP error。
- `backend/packages/harness/deerflow/persistence/migrations/versions/0001_project_saas_baseline.py` — 唯一 Alembic baseline。
- `backend/packages/harness/deerflow/persistence/bootstrap.py` — 空库初始化和旧库 fail-closed；不再包含 staged upgrade state machine。
- `backend/scripts/setup_postgres.py` — `setup-db`/future `migrate-db` CLI；旧 schema 返回 `M7_RECREATE_REQUIRED`。
- `backend/scripts/check_postgres.py` — 只检查 M7 head、final required tables/triggers 和 bootstrap catalog。

### System asset bootstrap

- `backend/app/shared_assets/bootstrap/__init__.py` — exports strict bootstrap public API。
- `backend/app/shared_assets/bootstrap/catalog.py` — manifest loader and digest validation。
- `backend/app/shared_assets/bootstrap/service.py` — single-transaction PostgreSQL seed。
- `backend/app/shared_assets/bootstrap/catalog.json` — versioned public metadata，不含 secret/credential/project binding。
- `backend/app/shared_assets/bootstrap/content/` — canonical built-in Agent/Skill/MCP content snapshots。
- `backend/app/shared_assets/catalog_provider.py` — PostgreSQL-only provider；删除 cutover boolean 和 file fallback。

### Final-only Gateway and project runtime

- `backend/app/gateway/deps.py` — `gateway_platform_runtime()` 只构造 final PostgreSQL/project services。
- `backend/app/private_work/http_runtime.py` — 从 legacy `gateway/services.py` 提取 `format_sse()` 和 `start_private_run()`。
- `backend/app/gateway/private_work_schemas.py` — project-private Run/token response schemas，不从 legacy router 导入。
- `backend/app/gateway/routers/private_work.py` — 唯一 Thread/Run/file/artifact/feedback/event HTTP runtime。
- `backend/app/gateway/routers/project_automations.py` — 唯一 Automation HTTP runtime。
- `backend/app/gateway/routers/project_input_polish.py` — project/owner-scoped composer polish。
- `backend/app/gateway/channel_schemas.py` — project connection provider/OAuth request/response models。

### Frontend final routes

- `frontend/src/core/private-work/` — 唯一 project Thread/file/memory/input-polish client 与 account/project query keys。
- `frontend/src/core/project-automations/schedule/` — 从 legacy scheduled-tasks 提取纯 cron/recipe code。
- `frontend/src/components/projects/private-work/` — 显式接收 project client/capability/URL builder。
- `frontend/src/app/workspace/page.tsx` — live 多项目 workspace；static build 在同一路径渲染无网络 demo。
- `frontend/src/app/projects/[project_slug]/`、`frontend/src/app/admin/` — 唯一 live project/admin 页面。

### Release gates

- `backend/tests/test_m7_final_schema_runtime.py` — marker-free readiness/API response contract。
- `backend/tests/test_m7_asset_bootstrap_postgres.py` — manifest、atomic seed、runtime-no-file-read gate。
- `backend/tests/test_m7_legacy_api_surface.py` — router/OpenAPI/import/config absence gate。
- `backend/tests/test_m7_final_baseline_postgres.py` — empty install、schema equality、old-schema fail-before-DDL。
- `backend/tests/test_m7_backup_restore_postgres.py` — M7 archive/restore and pre-M7 rejection。
- `frontend/tests/unit/m7-legacy-surface.test.tsx` — route/client/string absence and static `/workspace` contract。
- `frontend/tests/e2e/m7-project-only-routes.spec.ts` — project-first live/static browser gate。

---

### Task 1: 建立 marker-free final schema/readiness contract

**Files:**

- Create: `backend/app/final_schema.py`
- Modify: `backend/app/private_work/readiness_service.py`
- Modify: `backend/app/automations/readiness.py`
- Modify: `backend/app/reliability/process_readiness.py`
- Modify: `backend/app/reliability/readiness.py`
- Modify: `backend/app/reliability/models.py`
- Modify: `backend/app/gateway/automation_schemas.py`
- Modify: `backend/app/gateway/routers/project_automations.py`
- Modify: `backend/app/gateway/routers/admin_operations.py`
- Modify: `backend/app/gateway/routers/project_usage.py`
- Modify: `backend/app/gateway/routers/project_audit.py`
- Modify: `backend/app/worker/app.py`
- Modify: `backend/app/scheduler/app.py`
- Modify: `frontend/src/core/private-work/readiness.ts`
- Modify: `frontend/src/core/project-automations/readiness.ts`
- Modify: `frontend/src/core/project-automations/types.ts`
- Modify: `frontend/src/core/admin-operations/types.ts`
- Test: `backend/tests/test_m7_final_schema_runtime.py`
- Modify: `backend/tests/test_private_work_readiness_router.py`
- Modify: `backend/tests/test_automation_readiness.py`
- Modify: `backend/tests/test_m6_process_readiness.py`
- Modify: `frontend/tests/unit/core/private-work/readiness.test.ts`
- Modify: `frontend/tests/unit/core/project-automations/readiness.test.tsx`
- Modify: `frontend/tests/unit/m6-admin-operations.test.tsx`

**Interfaces:**

- Produces `M7_FINAL_SCHEMA_REVISION = "0001_project_saas_baseline"`、`PRE_RESET_SCHEMA_REVISION = "0015_project_reliability_finalize"`（仅 Tasks 1–7 过渡使用）、`FinalSchemaState`、`FinalSchemaProbe.read()`、`FinalSchemaProbe.require_ready()`。
- `FinalSchemaState` fields: `revision: str | None`, `missing_relations: tuple[str, ...]`, `ready: bool`。
- `FinalSchemaProbe.require_ready()` raises only `FinalSchemaRequired` or `FinalSchemaUnavailable`; domain services map them to existing safe `*_UNAVAILABLE` errors，不再返回 `*_CUTOVER`。
- Automation readiness response replaces `automation_cutover_ready` with `schema_ready`; process readiness replaces `cutover` with `schema_state`。

- [ ] **Step 1: 写 final-schema RED tests**

```python
async def test_final_schema_probe_ignores_cutover_markers(session):
    session.scalar.side_effect = ["0015_project_reliability_finalize", True]
    state = await FinalSchemaProbe(
        accepted_revisions=("0015_project_reliability_finalize",),
        required_relations=("projects", "jobs", "run_events"),
    ).read(session)
    assert state == FinalSchemaState(
        revision="0015_project_reliability_finalize",
        missing_relations=(),
        ready=True,
    )
    sql = " ".join(str(call.args[0]) for call in session.scalar.await_args_list)
    assert "cutover_state" not in sql


def test_public_readiness_contract_has_no_cutover_field():
    fields = AutomationReadinessResponse.model_fields
    assert "schema_ready" in fields
    assert "automation_cutover_ready" not in fields
    assert "cutover" not in ReliabilityReadiness.model_fields
```

另写 DB unavailable、wrong revision、missing required relation、Worker/Scheduler fail-fast 和 domain-safe error mapping cases。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m7_final_schema_runtime.py tests/test_private_work_readiness_router.py tests/test_automation_readiness.py tests/test_m6_process_readiness.py -q
cd ../frontend
pnpm test tests/unit/core/private-work/readiness.test.ts tests/unit/core/project-automations/readiness.test.tsx
```

Expected: FAIL because `app.final_schema`/`schema_ready`/`schema_state` do not exist and current services query marker tables.

- [ ] **Step 3: 实现通用 final-schema probe**

```python
M7_FINAL_SCHEMA_REVISION = "0001_project_saas_baseline"
PRE_RESET_SCHEMA_REVISION = "0015_project_reliability_finalize"
FINAL_REQUIRED_RELATIONS = (
    "projects",
    "project_memberships",
    "agents",
    "skills",
    "mcp_servers",
    "threads_meta",
    "runs",
    "scheduled_tasks",
    "scheduled_task_runs",
    "jobs",
    "run_events",
    "project_usage_ledger",
    "audit_logs",
    "deletion_tombstones",
    "restore_proofs",
)


@dataclass(frozen=True, slots=True)
class FinalSchemaState:
    revision: str | None
    missing_relations: tuple[str, ...]
    ready: bool


class FinalSchemaProbe:
    def __init__(
        self,
        *,
        accepted_revisions: tuple[str, ...] = (
            M7_FINAL_SCHEMA_REVISION,
            PRE_RESET_SCHEMA_REVISION,
        ),
        required_relations: tuple[str, ...] = FINAL_REQUIRED_RELATIONS,
    ) -> None:
        self._accepted_revisions = accepted_revisions
        self._required_relations = required_relations

    async def read(self, session: AsyncSession) -> FinalSchemaState:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        present = await session.scalar(
            text("SELECT array_agg(name ORDER BY name) FROM unnest(CAST(:names AS text[])) name WHERE to_regclass(name) IS NOT NULL"),
            {"names": list(self._required_relations)},
        )
        present_set = set(present or ())
        missing = tuple(name for name in self._required_relations if name not in present_set)
        return FinalSchemaState(
            revision=None if revision is None else str(revision),
            missing_relations=missing,
            ready=str(revision) in self._accepted_revisions and not missing,
        )
```

`require_ready()` catches only SQLAlchemy DB errors as `FinalSchemaUnavailable`; wrong shape raises `FinalSchemaRequired`。查询不得引用 marker、owner、project data 或 private content。

- [ ] **Step 4: 切换 backend/frontend public readiness fields**

```python
@dataclass(frozen=True, slots=True)
class AutomationReadiness:
    status: Literal["ready", "unavailable"]
    code: str
    scheduler_enabled: bool
    scheduler_status: SchedulerReadinessStatus
    project_private_work_ready: bool
    schema_ready: bool
    request_id: str
```

`PrivateWorkReadinessService`、Automation、usage/audit/admin operations、Worker、Scheduler 均通过同一个 probe；wrong schema 映射为 safe unavailable。Frontend Zod 同步拒绝旧字段，Admin Overview 渲染 `schema_state`。

- [ ] **Step 5: 运行 affected gates 并提交**

```bash
cd backend
uv run pytest tests/test_m7_final_schema_runtime.py tests/test_private_work_readiness_router.py tests/test_automation_readiness.py tests/test_m6_process_readiness.py tests/test_m6_system_operations_api_postgres.py -q
uvx ruff check app/final_schema.py app/private_work/readiness_service.py app/automations/readiness.py app/reliability app/gateway/automation_schemas.py app/gateway/routers/project_automations.py
cd ../frontend
pnpm test tests/unit/core/private-work/readiness.test.ts tests/unit/core/project-automations/readiness.test.tsx tests/unit/m6-admin-operations.test.tsx
pnpm check
cd ..
git add backend/app/final_schema.py backend/app/private_work/readiness_service.py backend/app/automations/readiness.py backend/app/reliability backend/app/gateway backend/tests/test_m7_final_schema_runtime.py backend/tests/test_private_work_readiness_router.py backend/tests/test_automation_readiness.py backend/tests/test_m6_process_readiness.py frontend/src/core frontend/tests AGENTS.md backend/AGENTS.md frontend/AGENTS.md
git commit -m "refactor: add M7 final schema readiness"
```

Expected: affected tests PASS; no public readiness response contains `cutover` or `automation_cutover_ready`。

### Task 2: 建立 deterministic system asset bootstrap并关闭 pre-cutover catalog

**Files:**

- Create: `backend/app/shared_assets/bootstrap/__init__.py`
- Create: `backend/app/shared_assets/bootstrap/catalog.py`
- Create: `backend/app/shared_assets/bootstrap/service.py`
- Create: `backend/app/shared_assets/bootstrap/catalog.json`
- Create: `backend/app/shared_assets/bootstrap/content/`
- Modify: `backend/app/shared_assets/catalog_provider.py`
- Modify: `backend/app/shared_assets/catalog_state_repository.py`
- Modify: `backend/packages/harness/deerflow/assets/catalog.py`
- Modify: `backend/packages/harness/deerflow/persistence/shared_assets/binding_model.py`
- Modify: `backend/packages/harness/deerflow/config/agents_config.py`
- Modify: `backend/packages/harness/deerflow/skills/storage/__init__.py`
- Modify: `backend/packages/harness/deerflow/skills/storage/local_skill_storage.py`
- Modify: `backend/packages/harness/deerflow/skills/storage/skill_storage.py`
- Delete: `backend/packages/harness/deerflow/skills/storage/user_scoped_skill_storage.py`
- Delete: `backend/packages/harness/deerflow/skills/installer.py`
- Modify: `backend/packages/harness/deerflow/skills/types.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py`
- Modify: `backend/packages/harness/deerflow/subagents/executor.py`
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py`
- Delete: `backend/packages/harness/deerflow/tools/skill_manage_tool.py`
- Modify: `backend/packages/harness/deerflow/mcp/cache.py`
- Modify: `backend/packages/harness/deerflow/mcp/tools.py`
- Modify: `backend/packages/harness/deerflow/client.py`
- Modify: `backend/app/gateway/app.py`
- Delete: `backend/app/gateway/routers/asset_catalog_compat.py`
- Delete: `backend/app/gateway/routers/agents.py`
- Delete: `backend/app/gateway/routers/skills.py`
- Delete: `backend/app/gateway/routers/mcp.py`
- Delete: `backend/app/gateway/routers/features.py`
- Test: `backend/tests/test_m7_asset_bootstrap_postgres.py`
- Modify: `backend/tests/test_asset_catalog_provider.py`
- Modify: `backend/tests/test_private_asset_runtime.py`
- Modify: `backend/tests/test_lead_agent_skills.py`
- Delete: `backend/tests/test_legacy_system_asset_runtime.py`
- Delete: `backend/tests/test_features_router.py`
- Delete: `backend/tests/test_custom_agent.py`
- Delete: `backend/tests/test_skills_custom_router.py`
- Delete: `backend/tests/test_skills_router_authz.py`
- Delete: `backend/tests/test_mcp_config_secrets.py`
- Delete: `backend/tests/test_user_scoped_skill_storage.py`
- Delete: `backend/tests/test_skills_installer.py`
- Delete: `backend/tests/test_local_skill_storage_write.py`
- Delete: `backend/tests/test_skill_manage_tool.py`
- Delete: `backend/tests/test_three_way_skills_mount_e2e.py`
- Delete: `backend/tests/blocking_io/test_agents_router.py`
- Delete: `backend/tests/blocking_io/test_skills_router.py`

**Interfaces:**

- Produces `BootstrapCatalog`, `BootstrapEntry`, `BootstrapResult`, `load_bootstrap_catalog()`, `bootstrap_system_assets(session_factory)`。
- Builtin writer identity is fixed to non-login principal `00000000-0000-0000-0000-000000000007` / `builtin-assets@deerflow.invalid`; it has no password/OAuth/project membership and cannot pass `resolve_asset_actor()`。
- `AssetCatalogStateRow` keeps only `id`, `generation`, `updated_at`; `cutover_at` and provider `is_cutover_enabled()` disappear。
- Harness catalog lookup always requires `PostgresAssetCatalogProvider`; absence is `AssetCatalogUnavailable`, never filesystem fallback。
- Run admission snapshot is the only source of enabled project/system Skill bytes。`SkillCategory.LEGACY`、per-user/global custom scan、default repo-root scan、extensions enablement and archive install/mutation disappear；temporary Skill files may only materialize the already-authorized immutable run snapshot。

- [ ] **Step 1: 写 manifest/atomicity/runtime-no-file RED tests**

```python
async def test_bootstrap_catalog_is_atomic_and_idempotent(m7_database):
    first = await bootstrap_system_assets(m7_database.session_factory)
    second = await bootstrap_system_assets(m7_database.session_factory)
    assert first.digest == second.digest
    assert first.counts == second.counts
    assert first.created > 0
    assert second.created == 0


async def test_runtime_catalog_has_no_cutover_or_filesystem_branch(provider):
    assert not hasattr(provider, "is_cutover_enabled")
    source = inspect.getsource(PostgresAssetCatalogProvider)
    assert "cutover" not in source
    assert "Path(" not in source
```

另写 unknown manifest key、digest mismatch、duplicate source key、transaction rollback、no credential/no binding、builtin principal non-login、Gateway/Worker/Scheduler startup不读 repo/user/custom/extensions assets 的 monkeypatch gate，以及 `SkillCategory.LEGACY`/`get_or_new_user_skill_storage`/archive installer import absence gate。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m7_asset_bootstrap_postgres.py tests/test_asset_catalog_provider.py tests/test_private_asset_runtime.py -q
```

Expected: FAIL because bootstrap package is absent and provider still exposes cutover/file fallback.

- [ ] **Step 3: 实现 strict bootstrap catalog**

```python
class BootstrapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_key: str = Field(pattern=r"^builtin:(agent|skill|mcp):[a-z0-9-]+$")
    kind: Literal["agent", "skill", "mcp"]
    slug: str
    display_name: str
    version: int = Field(ge=1)
    payload_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BootstrapCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    entries: tuple[BootstrapEntry, ...]


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    digest: str
    counts: Mapping[str, int]
    created: int
```

Loader uses `importlib.resources.files("app.shared_assets.bootstrap")`, resolves only manifest-listed relative paths, rejects `..`/symlink/non-regular files, verifies bytes before parsing。Seed opens one session/transaction, locks `asset_catalog_state`, inserts builtin principal if absent, writes published system rows/versions/files/refs and commits once；不得调用 service-owned commit。

- [ ] **Step 4: 删除 file-backed routers和永久化 provider**

```python
class AssetCatalogProvider(Protocol):
    async def get_system_agent(self, slug: str) -> AssetCatalogAgentSnapshot: ...
    async def list_system_agents(self) -> tuple[AssetCatalogAgentSnapshot, ...]: ...
    async def list_system_skills(self) -> tuple[AssetCatalogSkillSnapshot, ...]: ...
    async def list_system_mcp(self) -> tuple[AssetCatalogMcpSnapshot, ...]: ...
    async def materialize_mcp_secrets(
        self,
        context: object,
        snapshot: AssetCatalogMcpSnapshot,
    ) -> Mapping[str, Mapping[str, object]]: ...
```

删除 `is_cutover_enabled`、`cutover_at`、`ASSET_CATALOG_CUTOVER` 和 app router mounts。Refactor lead prompt、skill activation、subagent、sandbox and channel execution to consume the immutable run-owned asset snapshot; delete default/global/user storage lookup、legacy category、archive install and `skill_manage` file mutation。Project Skill creation stays only in authenticated project shared-asset HTTP service。

- [ ] **Step 5: 运行 gates 并提交**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m7_asset_bootstrap_postgres.py tests/test_asset_catalog_provider.py tests/test_private_asset_runtime.py tests/integration/test_m3_asset_resolution_postgres.py tests/test_harness_boundary.py -q
uvx ruff check app/shared_assets packages/harness/deerflow/assets packages/harness/deerflow/config/agents_config.py packages/harness/deerflow/skills packages/harness/deerflow/mcp
git add app/shared_assets app/gateway/app.py packages/harness/deerflow tests AGENTS.md ../AGENTS.md
git commit -m "refactor: make system assets PostgreSQL only"
```

Expected: asset bootstrap/integration PASS with 0 PostgreSQL skips; OpenAPI no `/api/agents`, `/api/skills`, `/api/mcp/config`, `/api/features`。

### Task 3: 移除 Gateway global private runtime和legacy Thread API

**Files:**

- Create: `backend/app/private_work/http_runtime.py`
- Modify: `backend/app/gateway/private_work_schemas.py`
- Modify: `backend/app/gateway/routers/private_work.py`
- Modify: `backend/app/private_work/connection_inbound.py`
- Modify: `backend/app/gateway/deps.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/services.py`
- Modify: `backend/app/gateway/langgraph_auth.py`
- Modify: `docker/nginx/nginx.conf`
- Modify: `docker/nginx/nginx.local.conf`
- Modify: `scripts/serve.sh`
- Modify: `scripts/docker.sh`
- Modify: `scripts/deploy.sh`
- Delete: `backend/app/gateway/routers/artifacts.py`
- Delete: `backend/app/gateway/routers/assistants_compat.py`
- Delete: `backend/app/gateway/routers/feedback.py`
- Delete: `backend/app/gateway/routers/memory.py`
- Delete: `backend/app/gateway/routers/runs.py`
- Delete: `backend/app/gateway/routers/suggestions.py`
- Delete: `backend/app/gateway/routers/thread_runs.py`
- Delete: `backend/app/gateway/routers/threads.py`
- Delete: `backend/app/gateway/routers/uploads.py`
- Test: `backend/tests/test_m7_legacy_api_surface.py`
- Modify: `backend/tests/test_private_work_router.py`
- Modify: `backend/tests/test_private_work_run_router.py`
- Modify: `backend/tests/test_private_work_stream_router.py`
- Modify: `backend/tests/test_private_work_file_router.py`
- Modify: `backend/tests/test_channel_runtime_identity.py`
- Delete: `backend/tests/test_artifacts_router.py`
- Delete: `backend/tests/test_memory_router.py`
- Delete: `backend/tests/test_runs_api_endpoints.py`
- Delete: `backend/tests/test_suggestions_router.py`
- Delete: `backend/tests/test_threads_router.py`
- Delete: `backend/tests/test_uploads_router.py`
- Delete: `backend/tests/test_legacy_thread_cutover_postgres.py`
- Delete: `backend/tests/test_stateless_runs_owner_isolation.py`
- Delete: `backend/tests/blocking_io/test_artifacts_router.py`
- Delete: `backend/tests/blocking_io/test_uploads_router.py`

**Interfaces:**

- `http_runtime.py` exports only `format_sse(event, data, *, event_id=None) -> str` and `start_private_run(body, thread_id, request, context) -> RunRecord`。
- `gateway_platform_runtime(app, startup_config)` keeps engine、raw checkpointer/store、project-scoped checkpointer、PostgreSQL private Run/event stores、quota/audit/asset/project services；it does not create `RunManager`, legacy stream bridge, configurable legacy RunStore/RunEventStore, scheduled legacy repos or orphan migration。
- `private_work_schemas.py` owns `PrivateRunCreateRequest` and `PrivateThreadTokenUsageResponse`; project router no longer imports deleted modules。

- [ ] **Step 1: 写 legacy surface absence RED test**

```python
LEGACY_PREFIXES = (
    "/api/threads",
    "/api/runs",
    "/api/assistants",
    "/api/memory",
)


def test_gateway_openapi_has_no_global_private_routes():
    paths = create_app().openapi()["paths"]
    assert all(
        not path.startswith(prefix)
        for path in paths
        for prefix in LEGACY_PREFIXES
    )


def test_gateway_runtime_has_no_legacy_execution_singletons():
    source = inspect.getsource(gateway_platform_runtime)
    for forbidden in ("RunManager", "make_stream_bridge", "make_run_event_store", "_migrate_orphaned_threads"):
        assert forbidden not in source
```

另写 project routes仍存在、project Run admission/SSE/file/feedback contract不变、Nginx无 `/api/langgraph` rewrite、deleted module import fails。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m7_legacy_api_surface.py tests/test_private_work_router.py tests/test_private_work_run_router.py tests/test_private_work_stream_router.py tests/test_private_work_file_router.py -q
```

Expected: FAIL because legacy routes and runtime singletons remain mounted.

- [ ] **Step 3: 提取 project-only schemas/runtime**

```python
class PrivateRunCreateRequest(StrictPrivateWorkRequest):
    assistant_id: str | None = None
    input: dict[str, object] | list[object] | str | None = None
    command: dict[str, object] | None = None
    config: dict[str, object] = Field(default_factory=dict)
    context: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject"


def format_sse(event: str, data: object, *, event_id: str | None = None) -> str:
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines.extend((f"event: {event}", f"data: {json.dumps(data, separators=(',', ':'))}"))
    return "\n".join(lines) + "\n\n"
```

Move only project admission/sanitization logic. Delete `start_run()`、stateless routes、legacy scheduled launcher、global regenerate/branch helpers。Channel inbound imports `start_private_run` directly from `app.private_work.http_runtime`。

- [ ] **Step 4: 简化 Gateway lifespan并删除 routers/rewrite**

`gateway_platform_runtime()` still initializes final services in one `AsyncExitStack`; shutdown closes engine/checkpointer/store after request drain but has no in-process Agent task drain。Remove old router imports/mounts、`/api/langgraph` Nginx locations and CLI output。`/api/projects/{id}/private-work/*` remains regular `/api/*` proxy traffic。

- [ ] **Step 5: 运行 affected gates并提交**

```bash
cd backend
uv run pytest tests/test_m7_legacy_api_surface.py tests/test_private_work_router.py tests/test_private_work_run_router.py tests/test_private_work_stream_router.py tests/test_private_work_file_router.py tests/test_channel_runtime_identity.py tests/test_m6_private_run_gateway.py tests/test_m6_gateway_reconnect_process.py -q
uv run pytest tests/blocking_io/test_gate_smoke.py tests/blocking_io/test_automations.py -q
uvx ruff check app/gateway app/private_work
cd ..
rg -n "/api/langgraph|make_stream_bridge|RunManager" backend/app/gateway docker/nginx scripts/serve.sh scripts/docker.sh scripts/deploy.sh
git add backend/app/gateway backend/app/private_work backend/tests docker/nginx scripts AGENTS.md backend/AGENTS.md
git commit -m "refactor: remove global private runtime"
```

Expected: pytest PASS; final `rg` returns no matches in scoped production files。

### Task 4: 删除 global Scheduled Tasks API，只保留 project Automation

**Files:**

- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/deps.py`
- Modify: `backend/app/gateway/automation_schemas.py`
- Modify: `backend/app/gateway/routers/project_automations.py`
- Modify: `backend/app/automations/errors.py`
- Modify: `backend/app/automations/error_mapping.py`
- Modify: `backend/app/automations/service.py`
- Modify: `backend/app/scheduler/service.py`
- Modify: `backend/app/scheduler/app.py`
- Delete: `backend/app/gateway/routers/scheduled_tasks.py`
- Delete: `backend/app/automations/legacy_reads.py`
- Delete: `backend/app/automations/cutover.py`
- Test: `backend/tests/test_m7_legacy_api_surface.py`
- Modify: `backend/tests/test_project_automations_router.py`
- Modify: `backend/tests/test_project_automation_service.py`
- Modify: `backend/tests/test_automation_dispatcher.py`
- Modify: `backend/tests/test_automation_scheduler_ownership.py`
- Modify: `backend/tests/test_scheduled_task_service.py`
- Delete: `backend/tests/test_scheduled_task_router.py`
- Delete: `backend/tests/test_scheduled_task_router_behavior.py`
- Delete: `backend/tests/test_legacy_automation_reads_postgres.py`
- Delete: `backend/tests/test_automation_cutover.py`
- Delete: `frontend/tests/e2e/scheduled-tasks.spec.ts`

**Interfaces:**

- PostgreSQL tables/repositories `scheduled_tasks` and `scheduled_task_runs` stay as final Automation persistence；只有 global HTTP/read adapter 删除。
- Rename service class `ScheduledTaskService` to `AutomationSchedulerService`; its public methods are `reconcile_admitted_runs()` and `admit_due_occurrences(now)`，不接受 account/project/owner from client。
- Remove `AUTOMATION_CUTOVER_REQUIRED`、`AUTOMATION_LEGACY_READ_ONLY`、`require_legacy_automation_read()` and all marker-derived branches。
- `/api/projects/{project_id}/automations*` remains the only Automation surface；`/api/scheduled-tasks*` returns ordinary `404`。

- [ ] **Step 1: 写 route/service RED tests**

```python
def test_only_project_automation_routes_are_mounted():
    paths = create_app().openapi()["paths"]
    assert not any(path.startswith("/api/scheduled-tasks") for path in paths)
    assert "/api/projects/{project_id}/automations" in paths


def test_automation_error_contract_has_no_cutover_codes():
    source = inspect.getsource(automation_error_response)
    assert "CUTOVER" not in source
    assert "LEGACY" not in source
```

Add project-role matrix、owner isolation、manual atomic occurrence/Run/job admission、Scheduler same-transaction admission、restart terminal reconciliation and ownership-loss exit cases。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m7_legacy_api_surface.py tests/test_project_automations_router.py tests/test_project_automation_service.py tests/test_automation_scheduler_ownership.py tests/test_scheduled_task_service.py -q
```

Expected: FAIL because global router、legacy reader、cutover error and old service name remain。

- [ ] **Step 3: 收敛 Automation service and error contract**

Final public signatures are `AutomationSchedulerService.reconcile_admitted_runs(session: AsyncSession) -> int` and `AutomationSchedulerService.admit_due_occurrences(session: AsyncSession, *, now: datetime) -> tuple[AutomationOccurrence, ...]`。Both methods keep caller-owned transactions and never commit independently。

Remove legacy route dependencies and reads. `ProjectAutomationService` continues resolving authenticated account、immutable `ProjectContext` and owner-scoped repository internally。Client `context.non_interactive` is still dropped; dispatcher writes it as server-owned admission data。

- [ ] **Step 4: 删除 global router/cutover modules and rename Scheduler wiring**

Update Gateway/OpenAPI、Scheduler app factory、reload-boundary test imports and docs。Do not rename database tables in this task because their names are private persistence, not an exposed legacy API。

- [ ] **Step 5: 运行 gates并提交**

```bash
cd backend
uv run pytest tests/test_m7_legacy_api_surface.py tests/test_project_automations_router.py tests/test_project_automation_service.py tests/test_automation_dispatcher.py tests/test_automation_scheduler_ownership.py tests/test_scheduled_task_service.py tests/test_automation_reconciliation.py tests/test_m6_automation_job_admission_postgres.py -q
uvx ruff check app/automations app/scheduler app/gateway/automation_schemas.py app/gateway/routers/project_automations.py
cd ..
rg -n "AUTOMATION_CUTOVER|legacy_reads|/api/scheduled-tasks|ScheduledTaskService" backend/app backend/tests
git add backend/app backend/tests frontend/tests/e2e/scheduled-tasks.spec.ts AGENTS.md backend/AGENTS.md
git commit -m "refactor: remove legacy automation API"
```

Expected: tests PASS; final `rg` returns no production matches，deleted URL is `404`。

### Task 5: 让 channel/input-polish 只接受显式 project authority

**Files:**

- Create: `backend/app/gateway/channel_schemas.py`
- Create: `backend/app/gateway/routers/project_input_polish.py`
- Modify: `backend/app/gateway/deps.py`
- Modify: `backend/app/gateway/routers/project_connections.py`
- Modify: `backend/packages/harness/deerflow/persistence/channel_connections/__init__.py`
- Delete: `backend/packages/harness/deerflow/persistence/channel_connections/legacy_sql.py`
- Modify: `backend/app/channels/connection_identity.py`
- Modify: `backend/app/channels/manager.py`
- Modify: `backend/app/channels/run_policy.py`
- Modify: `backend/app/channels/runtime_config_store.py`
- Modify: `backend/app/private_work/connection_inbound.py`
- Modify: `backend/app/reliability/operations.py`
- Modify: `backend/app/gateway/routers/admin_operations.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `frontend/src/core/input-polish/api.ts`
- Modify: `frontend/src/components/workspace/input-box.tsx`
- Modify: `frontend/src/components/projects/private-work/project-connections-page.tsx`
- Delete: `backend/app/gateway/routers/channel_connections.py`
- Delete: `backend/app/gateway/routers/channels.py`
- Delete: `backend/app/gateway/routers/console.py`
- Delete: `backend/app/gateway/routers/input_polish.py`
- Test: `backend/tests/test_m7_project_channel_authority.py`
- Modify: `backend/tests/test_channel_runtime_identity.py`
- Modify: `backend/tests/test_channel_runtime_worker_scope.py`
- Modify: `backend/tests/test_channel_connections_router.py`
- Modify: `backend/tests/test_channel_connections_repository.py`
- Modify: `backend/tests/test_input_polish_router.py`
- Delete: `backend/tests/test_channel_connections_cutover_router.py`
- Delete: `backend/tests/test_channels_router.py`
- Delete: `backend/tests/test_console_router.py`
- Delete: `frontend/tests/e2e/channels.spec.ts`

**Interfaces:**

- `ProjectConnectionProviderResponse` and OAuth/request models live in `gateway/channel_schemas.py`; project router never imports a deleted global router。
- Channel inbound identity is exactly `(account_id, project_id, owner_user_id, connection_id)` loaded from the PostgreSQL connection row；no default project、recent project、unique membership or environment fallback。
- Input polish endpoint becomes `POST /api/projects/{project_id}/private-work/input-polish`; it requires authenticated member、`private_work.create` and approved executable asset snapshot。
- `project_input_polish_context` validates both `private_work.create` and `shared_assets.execute`; request-supplied project/owner/capability/snapshot/grant fields are ignored or rejected before the auxiliary model call。
- System operations expose only aggregate provider health (`provider`, `status`, `checked_at`, safe `code`)；never expose token、webhook secret、external account identifier or connection payload。

- [ ] **Step 1: 写 explicit-authority RED tests**

```python
@pytest.mark.parametrize(
    "path",
    ("/api/channels", "/api/channel-connections", "/api/console", "/api/input-polish"),
)
async def test_global_channel_and_polish_routes_are_gone(client, path):
    response = await client.get(path)
    assert response.status_code == 404


async def test_inbound_never_guesses_project(channel_runtime):
    with pytest.raises(ChannelIdentityUnavailable):
        await channel_runtime.resolve_without_connection_binding(external_user_id="u-1")
```

Add connection owner mismatch、removed member、deleted project、disabled/revoked connection、viewer polish denial、snapshot/credential grant verification and admin aggregate redaction cases。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m7_project_channel_authority.py tests/test_channel_runtime_identity.py tests/test_channel_runtime_worker_scope.py tests/test_channel_connections_router.py tests/test_input_polish_router.py -q
```

Expected: FAIL because global routers and implicit identity branches still exist。

- [ ] **Step 3: 提取 schemas and project polish route**

```python
@router.post("/{project_id}/private-work/input-polish")
async def polish_project_input(
    project_id: UUID,
    body: ProjectInputPolishRequest,
    context: ProjectContext = Depends(project_input_polish_context),
    service: ProjectInputPolishService = Depends(project_input_polish_service),
) -> ProjectInputPolishResponse:
    return await service.polish(context=context, body=body)
```

Service resolves the Agent snapshot and credential grants server-side, then performs only the scoped input-polish auxiliary model call；it never constructs or executes an Agent graph。

- [ ] **Step 4: 删除 implicit fallback and global routers**

Move model imports first, then remove mounts/files and `LegacyChannelConnectionRepository` export/implementation。Frontend private-work client builds URL exclusively from the active project client; no unscoped fallback。Admin operations use one bounded provider-health query and safe enum mapping。

- [ ] **Step 5: 运行 gates并提交**

```bash
cd backend
uv run pytest tests/test_m7_project_channel_authority.py tests/test_channel_runtime_identity.py tests/test_channel_runtime_worker_scope.py tests/test_channel_connections_router.py tests/test_channel_connections_repository.py tests/test_input_polish_router.py tests/test_slack_channel_connections.py tests/test_telegram_channel_connections.py tests/test_m6_system_operations_api_postgres.py -q
uv run pytest tests/blocking_io/test_channels_ingest.py tests/blocking_io/test_channel_runtime_config_store.py -q
uvx ruff check app/channels app/private_work/connection_inbound.py app/gateway/channel_schemas.py app/gateway/routers/project_connections.py app/gateway/routers/project_input_polish.py
cd ../frontend
pnpm test tests/unit/components/projects tests/unit/core/private-work
pnpm check
cd ..
rg -n "default.project|recent.project|unique.membership|/api/(channels|channel-connections|console|input-polish)" backend/app frontend/src
git add backend/app backend/tests frontend/src frontend/tests AGENTS.md backend/AGENTS.md frontend/AGENTS.md
git commit -m "refactor: require project channel authority"
```

Expected: gates PASS; scoped `rg` has no implicit-project or global route matches。

### Task 6: 删除 frontend legacy workspace routes和global clients

**Files:**

- Create: `frontend/src/core/project-automations/schedule/cron.ts`
- Create: `frontend/src/core/project-automations/schedule/recipes.ts`
- Create: `frontend/src/core/project-automations/schedule/types.ts`
- Create: `frontend/src/core/private-work/connection-types.ts`
- Create: `frontend/src/core/private-work/connect-poll.ts`
- Create: `frontend/src/core/private-work/provider-state.ts`
- Create: `frontend/src/core/private-work/open-connect-url.ts`
- Create: `frontend/src/components/projects/automations/automation-schedule-input.tsx`
- Modify: `frontend/src/app/workspace/page.tsx`
- Modify: `frontend/src/app/projects/[project_slug]/layout.tsx`
- Modify: `frontend/src/components/workspace/chats/scoped-chat-page.tsx`
- Modify: `frontend/src/components/workspace/chats/use-thread-chat.ts`
- Modify: `frontend/src/components/workspace/input-box.tsx`
- Modify: `frontend/src/components/workspace/workspace-sidebar.tsx`
- Modify: `frontend/src/components/workspace/settings/settings-dialog.tsx`
- Modify: `frontend/src/components/ai-elements/artifact.tsx`
- Modify: `frontend/src/components/projects/automations/automation-form.tsx`
- Modify: `frontend/src/components/projects/private-work/project-connections-page.tsx`
- Modify: `frontend/src/core/private-work/memory.ts`
- Modify: `frontend/src/core/private-work/connections.ts`
- Modify: `frontend/src/core/threads/utils.ts`
- Modify: `frontend/src/core/project-automations/api.ts`
- Modify: `frontend/src/core/project-automations/types.ts`
- Delete: `frontend/src/app/workspace/chats/`
- Delete: `frontend/src/app/workspace/agents/`
- Delete: `frontend/src/app/workspace/memory/`
- Delete: `frontend/src/app/workspace/scheduled-tasks/`
- Delete: `frontend/src/app/workspace/skills/`
- Delete: `frontend/src/app/workspace/tools/`
- Delete: `frontend/src/app/workspace/projects/`
- Delete: `frontend/src/app/api/memory/`
- Delete: `frontend/src/core/agents/`
- Delete: `frontend/src/core/mcp/`
- Delete: `frontend/src/core/memory/`
- Delete: `frontend/src/core/scheduled-tasks/`
- Delete: `frontend/src/core/channels/`
- Delete: `frontend/src/core/skills/api.ts`
- Delete: `frontend/src/core/skills/hooks.ts`
- Delete: `frontend/src/core/skills/type.ts`
- Delete: `frontend/src/components/workspace/channels/`
- Delete: `frontend/src/components/workspace/settings/channels-settings-page.tsx`
- Delete: `frontend/src/components/workspace/thread-scheduled-tasks-link.tsx`
- Delete: `frontend/src/components/workspace/scheduled-task-schedule-input.tsx`
- Test: `frontend/tests/unit/m7-legacy-surface.test.tsx`
- Test: `frontend/tests/unit/core/private-work/connection-helpers.test.ts`
- Test: `frontend/tests/unit/core/project-automations/schedule.test.ts`
- Test: `frontend/tests/e2e/m7-project-only-routes.spec.ts`
- Modify: `frontend/tests/unit/components/projects/automations/automation-form.test.tsx`
- Delete: `frontend/tests/e2e/agents-feature-disabled.spec.ts`
- Delete: `frontend/tests/e2e/memory-workbench.spec.ts`
- Delete: `frontend/tests/unit/core/scheduled-tasks/`
- Delete: `frontend/tests/unit/core/channels/`
- Delete: `frontend/tests/unit/core/agents/`
- Delete: `frontend/tests/unit/core/mcp/`

**Interfaces:**

- `/workspace` is the only workspace route and renders multi-project cards when live；`BUILD_MODE=static` renders a local no-network demo at the same URL。
- Live chat route is only `/projects/{project_slug}/chats/{thread_id?}` and `ScopedChatPage` requires `ProjectPrivateWorkScope`; remove `LEGACY_WORKSPACE_CHAT_SCOPE` and optional project fallback。
- Pure cron validation/recipe constants move under `core/project-automations/schedule`; they contain no URL、fetch、auth or query key。
- Project connection types/poll/provider-state/open-URL helpers move under `core/private-work`; global channel API/hooks/query keys and workspace settings/sidebar entry disappear。
- Memory、Agent、Skill、MCP runtime calls only use project-private/shared-asset clients; artifact “install globally” actions disappear。

- [ ] **Step 1: 写 route/client absence RED tests**

```tsx
it("contains no live legacy workspace route or global API literal", () => {
  const productionFiles = readFrontendProductionSources();
  for (const forbidden of [
    "/workspace/chats",
    "/workspace/agents",
    "/workspace/memory",
    "/workspace/scheduled-tasks",
    "/api/memory",
    "/api/agents",
    "/api/skills",
    "/api/mcp/config",
  ]) {
    expect(productionFiles).not.toContain(forbidden);
  }
});
```

Browser cases: old URLs render Next not-found without redirect; project chat/memory/connections/automations render under the selected project; static `/workspace` sends zero `/api/` requests。

- [ ] **Step 2: 运行 RED tests**

```bash
cd frontend
pnpm test tests/unit/m7-legacy-surface.test.tsx tests/unit/components/projects/automations/automation-form.test.tsx
pnpm exec playwright test tests/e2e/m7-project-only-routes.spec.ts
```

Expected: FAIL because legacy route trees and global clients remain。

- [ ] **Step 3: 先移动 pure schedule/private-work code and fix imports**

```ts
export type ProjectAutomationSchedule =
  | { kind: "cron"; expression: string; timezone: string }
  | { kind: "interval"; everySeconds: number };

export function validateProjectAutomationSchedule(
  schedule: ProjectAutomationSchedule,
): ScheduleValidationResult { /* pure validation only */ }
```

Move types/recipes/schedule input and project connection pure helpers with history-preserving `git mv` during implementation, update project component/tests, then delete global API/hooks。Memory types used by project pages move beside `core/private-work/memory.ts` instead of importing deleted `core/memory`。

- [ ] **Step 4: 删除 route trees and make scope mandatory**

Remove compatibility components/providers、Thread scheduled-task link、workspace channel settings/sidebar entry and every fallback branch。Static adapter is selected at build time and cannot import authenticated API modules。Project layout remains server-authorized and derives all URLs from `ProjectPrivateWorkProvider`。Account/project transition tests must prove cancel occurs before cache clear and late responses cannot repopulate the new scope。

- [ ] **Step 5: 运行 frontend gates并提交**

```bash
cd frontend
pnpm test tests/unit/m7-legacy-surface.test.tsx tests/unit/core/project-automations tests/unit/core/private-work tests/unit/components/projects tests/unit/components/workspace
pnpm exec playwright test tests/e2e/m7-project-only-routes.spec.ts tests/e2e/project-automations.spec.ts
pnpm exec playwright test --config playwright.static.config.ts tests/e2e-static/project-automation-static.spec.ts
pnpm check
BUILD_MODE=production pnpm build
BUILD_MODE=static pnpm build
rg -n "/workspace/(chats|agents|memory|scheduled-tasks|skills|tools|projects)|/api/(memory|agents|skills|mcp/config)|LEGACY_WORKSPACE_CHAT_SCOPE" src
cd ..
git add frontend/src frontend/tests frontend/AGENTS.md README.md
git commit -m "refactor: remove legacy workspace surfaces"
```

Expected: tests/check/two builds PASS; final `rg` returns no production matches。

### Task 7: 删除 legacy config、extensions file authority和production fallback stores

**Files:**

- Modify: `backend/packages/harness/deerflow/config/app_config.py`
- Modify: `backend/packages/harness/deerflow/config/__init__.py`
- Modify: `backend/packages/harness/deerflow/config/channel_connections_config.py`
- Modify: `backend/packages/harness/deerflow/config/reload_boundary.py`
- Modify: `backend/packages/harness/deerflow/runtime/events/store/__init__.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/store/__init__.py`
- Modify: `backend/packages/harness/deerflow/runtime/__init__.py`
- Modify: `backend/app/channels/runtime_config_store.py`
- Modify: `config.example.yaml`
- Modify: `Makefile`
- Modify: `scripts/configure.py`
- Modify: `scripts/doctor.py`
- Modify: `scripts/support_bundle.py`
- Modify: `scripts/setup_wizard.py`
- Modify: `scripts/wizard/steps/channels.py`
- Modify: `docker/docker-compose.yaml`
- Modify: `docker/docker-compose-dev.yaml`
- Delete: `extensions_config.example.json`
- Delete: `backend/packages/harness/deerflow/config/extensions_config.py`
- Delete: `backend/packages/harness/deerflow/config/agents_api_config.py`
- Delete: `backend/packages/harness/deerflow/config/run_events_config.py`
- Delete: `backend/packages/harness/deerflow/config/stream_bridge_config.py`
- Delete: `backend/packages/harness/deerflow/runtime/events/store/memory.py`
- Delete: `backend/packages/harness/deerflow/runtime/runs/store/memory.py`
- Delete: `backend/packages/harness/deerflow/runtime/stream_bridge/`
- Test: `backend/tests/test_m7_config_contract.py`
- Modify: `backend/tests/test_app_config_reload.py`
- Modify: `backend/tests/test_reload_boundary.py`
- Delete: `backend/tests/test_stream_bridge.py`
- Delete: `backend/tests/test_m6_reliability_config.py`

**Interfaces:**

- Resolution order is explicit `DEER_FLOW_CONFIG_PATH` then `${REPO_ROOT}/config.yaml`; no current-working-directory、home、extensions or JSON probe。
- Pydantic `model_validator(mode="before")` rejects exact top-level tombstones: `agents_api`, `run_events`, `stream_bridge`, `extensions`, `extensions_config`, `mcp_config`, `mcp_config_path`, `legacy_run_store`, `legacy_event_store`。
- Final PostgreSQL event/run stores are constructed directly from `database.url`; no config enum or factory supports memory/redis/file。
- `channel_connections_config.py` stays only if final project connection provider settings still import it after Task 5；its legacy global store/path fields must be removed and tombstoned。

- [ ] **Step 1: 写 strict config/source absence RED tests**

```python
@pytest.mark.parametrize(
    "payload",
    (
        {"agents_api": {}},
        {"run_events": {"backend": "memory"}},
        {"stream_bridge": {"backend": "redis"}},
        {"extensions": {}},
        {"extensions_config": "./extensions_config.json"},
        {"mcp_config": "./mcp_config.json"},
    ),
)
def test_legacy_config_tombstones_are_rejected(payload):
    with pytest.raises(ValidationError, match="LEGACY_CONFIG_REMOVED"):
        AppConfig.model_validate(payload)
```

Add canonical-path precedence、missing config、unknown non-tombstone key、Docker env absence and import-failure tests for deleted runtime/config modules。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m7_config_contract.py tests/test_app_config_reload.py tests/test_reload_boundary.py -q
```

Expected: FAIL because deleted keys and memory/redis/file factories are still accepted。

- [ ] **Step 3: 实现 exact tombstone validator and direct PostgreSQL wiring**

```python
LEGACY_CONFIG_TOMBSTONES = frozenset(
    {
        "agents_api",
        "run_events",
        "stream_bridge",
        "extensions",
        "extensions_config",
        "mcp_config",
        "mcp_config_path",
        "legacy_run_store",
        "legacy_event_store",
    }
)


@model_validator(mode="before")
@classmethod
def reject_removed_legacy_config(cls, value: object) -> object:
    if isinstance(value, Mapping):
        removed = sorted(LEGACY_CONFIG_TOMBSTONES.intersection(value))
        if removed:
            raise ValueError(f"LEGACY_CONFIG_REMOVED: {','.join(removed)}")
    return value
```

Do not substring-match legitimate final keys. Update Gateway/Worker/Scheduler constructors to instantiate only PostgreSQL implementations; missing DB config fails startup。

- [ ] **Step 4: 删除 files、examples、wizard/Docker options**

`make config` copies only `config.example.yaml` when absent。Support bundle reports safe final config presence but does not discover extensions/MCP paths。Remove Redis stream env/volumes and legacy backend prompts；retain `channel_connections_config.py` as final provider enablement config, update its documentation/reload-boundary text to project connections, and do not remove unrelated channel provider credentials。

- [ ] **Step 5: 运行 gates并提交**

```bash
cd backend
uv run pytest tests/test_m7_config_contract.py tests/test_app_config_reload.py tests/test_reload_boundary.py tests/test_m6_gateway_reconnect_process.py tests/test_m6_worker_app.py tests/test_automation_app_wiring.py -q
uvx ruff check packages/harness/deerflow/config packages/harness/deerflow/runtime app/channels/runtime_config_store.py
cd ..
make doctor
rg -n "extensions_config|agents_api|run_events:|stream_bridge:|backend: (memory|redis)|make_stream_bridge|MemoryRun|MemoryEvent" backend/app backend/packages config.example.yaml Makefile scripts docker
git add backend/packages backend/app backend/tests config.example.yaml extensions_config.example.json Makefile scripts docker AGENTS.md backend/AGENTS.md
git commit -m "refactor: remove legacy runtime configuration"
```

Expected: tests/doctor PASS; final `rg` returns no production matches except explicit tombstone names in validator/tests。

### Task 8: 把 Alembic 重置为一个 final baseline并删除 migration CLIs

**Files:**

- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/0001_project_saas_baseline.py`
- Modify: `backend/packages/harness/deerflow/persistence/bootstrap.py`
- Modify: `backend/packages/harness/deerflow/persistence/migrations/env.py`
- Modify: `backend/scripts/setup_postgres.py`
- Modify: `backend/scripts/check_postgres.py`
- Modify: `backend/app/final_schema.py`
- Modify: `Makefile`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0001_baseline.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0002_runs_token_usage.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0003_scheduled_tasks.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0004_migration_ledger.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0005_project_foundation.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0006_project_governance.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0007_project_shared_assets.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0008_project_private_work_expand.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0009_project_private_work_finalize.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0010_private_file_source.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0011_private_artifact_tombstone.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0012_project_automation_expand.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0013_project_automation_finalize.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0014_project_reliability_expand.py`
- Delete: `backend/packages/harness/deerflow/persistence/migrations/versions/0015_project_reliability_finalize.py`
- Delete: `backend/packages/harness/deerflow/persistence/migration_ledger/`
- Delete: `backend/packages/harness/deerflow/persistence/automations/migration_digest.py`
- Delete: `backend/scripts/migrate_sqlite_to_postgres.py`
- Delete: `backend/scripts/migrate_user_isolation.py`
- Delete: `backend/scripts/migrate_assets.py`
- Delete: `backend/scripts/migrate_private_work.py`
- Delete: `backend/scripts/migrate_automations.py`
- Delete: `backend/scripts/migrate_reliability.py`
- Delete: `backend/scripts/sqlite_inventory.py`
- Test: `backend/tests/test_m7_final_baseline_postgres.py`
- Modify: `backend/tests/test_setup_postgres.py`
- Modify: `backend/tests/test_check_postgres.py`
- Modify: `backend/tests/test_persistence_bootstrap.py`
- Modify: `backend/tests/test_persistence_migrations_env.py`
- Delete: `backend/tests/test_sqlite_to_postgres_migration.py`
- Delete: `backend/tests/test_migration_user_isolation.py`
- Delete: `backend/tests/test_migrate_assets.py`
- Delete: `backend/tests/test_private_work_migration.py`
- Delete: `backend/tests/test_private_work_migration_cli.py`
- Delete: `backend/tests/test_automation_migration.py`
- Delete: `backend/tests/test_automation_migration_cli.py`
- Delete: `backend/tests/test_m6_reliability_migration_postgres.py`
- Delete: `backend/tests/test_m6_reliability_migration_proof.py`
- Delete: `backend/tests/integration/test_m3_asset_migration_postgres.py`
- Delete: `backend/tests/integration/test_m4_private_work_migration_postgres.py`
- Delete: `backend/tests/integration/test_m5_automation_migration_postgres.py`

**Interfaces:**

- Alembic graph has exactly one revision: `revision = "0001_project_saas_baseline"`, `down_revision = None`；`downgrade()` raises with explicit unsupported message。
- Baseline creates only final runtime tables/indexes/FKs/checks/triggers/functions and never creates `migration_ledger`、domain cutover marker、legacy owner/source columns or temporary proof tables。
- Bootstrap classification: truly empty schema → upgrade to head and seed builtin catalog；exact M7 head → idempotent verify/seed；old Alembic revision or unknown non-empty schema → fail before DDL with `M7_RECREATE_REQUIRED`。
- Remove `PRE_RESET_SCHEMA_REVISION`; final probe accepts only `M7_FINAL_SCHEMA_REVISION`。

- [ ] **Step 1: 写 baseline/reset RED tests**

```python
async def test_empty_database_installs_exact_m7_baseline(postgres_server):
    database = await postgres_server.create_random_database("deerflow_test_m7_")
    await setup_database(database.url)
    assert await database.scalar("SELECT version_num FROM alembic_version") == "0001_project_saas_baseline"
    assert not await database.has_relation("migration_ledger")
    assert not await database.has_relation("private_work_cutover_state")


async def test_old_revision_is_rejected_before_any_ddl(postgres_server):
    database = await postgres_server.create_random_database("deerflow_test_m7_old_")
    await database.execute("CREATE TABLE alembic_version (version_num varchar(64) PRIMARY KEY)")
    await database.execute("INSERT INTO alembic_version VALUES ('0015_project_reliability_finalize')")
    before = await database.schema_digest()
    with pytest.raises(M7RecreateRequired):
        await setup_database(database.url)
    assert await database.schema_digest() == before
```

Also test unknown non-empty schema、empty concurrent setup、required triggers/functions、builtin catalog idempotency、metadata-to-baseline table/column/index/check/FK equality and no deleted marker/source columns。

- [ ] **Step 2: 运行 RED tests against real PostgreSQL**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m7_final_baseline_postgres.py tests/test_setup_postgres.py tests/test_check_postgres.py tests/test_persistence_bootstrap.py -q
```

Expected: FAIL because old chain and staged bootstrap still exist。

- [ ] **Step 3: 生成并审计 single baseline**

Use the final SQLAlchemy metadata as input, then hand-review generated DDL against all final repositories。The revision must explicitly install non-metadata functions/triggers for quota、audit、stream terminal invariants、retention/recovery and updated-at behavior。Never import application models from the revision at runtime。

```python
revision = "0001_project_saas_baseline"
down_revision = None
branch_labels = None
depends_on = None


def downgrade() -> None:
    raise RuntimeError("M7 baseline downgrade is unsupported; recreate a new database")
```

- [ ] **Step 4: 实现 preflight-before-DDL bootstrap**

```python
class M7RecreateRequired(RuntimeError):
    code = "M7_RECREATE_REQUIRED"


async def classify_database(connection: AsyncConnection) -> Literal["empty", "m7"]:
    relations = await list_user_relations(connection)
    if not relations:
        return "empty"
    revision = await read_revision_if_present(connection)
    if revision == M7_FINAL_SCHEMA_REVISION and await has_final_required_relations(connection):
        return "m7"
    raise M7RecreateRequired("existing pre-M7 or unknown schema must be recreated manually")
```

Classification runs on a connection before Alembic or seed code can mutate state。`setup-db` prints manual create-new-database guidance only; it never drops the target。`migrate-db` becomes an alias for exact-head verification/future forward migrations, not an old-chain upgrader。

- [ ] **Step 5: 删除 migration code/tests/targets and run gates**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m7_final_baseline_postgres.py tests/test_setup_postgres.py tests/test_check_postgres.py tests/test_persistence_bootstrap.py tests/test_persistence_bootstrap_concurrency.py tests/test_persistence_migrations_env.py -q
uvx ruff check packages/harness/deerflow/persistence app/final_schema.py scripts/setup_postgres.py scripts/check_postgres.py
cd ..
rg -n "001[0-5]_project|000[1-9]_(baseline|project|scheduled|migration)|migration_ledger|cutover_state|migrate-(sqlite|assets|private-work|automations|reliability)" backend/app backend/packages backend/scripts Makefile
git add backend/packages/harness/deerflow/persistence backend/scripts backend/app/final_schema.py backend/tests Makefile AGENTS.md backend/AGENTS.md
git commit -m "refactor: reset database to M7 baseline"
```

Expected: real PostgreSQL gate PASS with 0 skips; migrations directory has one revision; scoped `rg` finds only intentional error tests/history exclusions。

### Task 9: 让 backup/restore只接受 M7 archive

**Files:**

- Modify: `backend/app/recovery/archive.py`
- Modify: `backend/app/recovery/restore.py`
- Modify: `backend/app/recovery/restore_process.py`
- Modify: `backend/app/recovery/purge.py`
- Modify: `backend/app/recovery/journal.py`
- Modify: `backend/scripts/backup_postgres.py`
- Modify: `backend/scripts/restore_postgres.py`
- Modify: `backend/scripts/drill_restore.py`
- Modify: `docs/operations/m6-backup-recovery.md`
- Delete: `backend/app/recovery/pre_cutover_backup.py`
- Test: `backend/tests/test_m7_backup_restore_postgres.py`
- Modify: `backend/tests/test_m6_backup_archive.py`
- Modify: `backend/tests/test_m6_restore_postgres.py`
- Modify: `backend/tests/test_m6_restore_safety.py`
- Delete: `backend/tests/test_m6_pre_cutover_backup.py`
- Modify: `backend/tests/blocking_io/test_m6_backup_subprocess.py`
- Modify: `backend/tests/blocking_io/test_m6_restore_workflow.py`

**Interfaces:**

- Archive manifest adds `archive_schema_version: Literal[7]` and `schema_digest: str`, and requires `schema_revision == "0001_project_saas_baseline"`；authenticated AAD binds version、revision、schema digest、source identity and chunk index。
- Backup rejects any source not at exact M7 final schema before `pg_dump` and does not expose a `--pre-m6-cutover` option。
- Restore rejects pre-M7/unknown manifest as `UNSUPPORTED_ARCHIVE_SCHEMA` before target creation/DDL；it still requires a new `deerflow_restore_*` database and never switches `DATABASE_URL`。
- Existing journal-first purge、PostgreSQL head/source anchor、continuous tombstone replay、proof digest/final sequence、inode cleanup/fsync and drill ownership handoff semantics remain unchanged。

- [ ] **Step 1: 写 archive-version RED tests**

```python
def test_archive_manifest_requires_m7_schema():
    with pytest.raises(UnsupportedArchiveSchema):
        ArchiveManifest.model_validate(
            valid_manifest_payload(
                archive_schema_version=6,
                schema_revision="0015_project_reliability_finalize",
            )
        )


async def test_restore_rejects_pre_m7_before_target_creation(restore_harness):
    archive = restore_harness.authenticated_pre_m7_archive()
    with pytest.raises(UnsupportedArchiveSchema):
        await restore_harness.restore(archive)
    assert restore_harness.created_targets == []
```

Add tampered version/revision/schema-digest AAD、wrong source head、runtime-schema mismatch、journal gap、body failure cleanup、cancel-after-unlock cleanup、proof binding and successful random drill cleanup cases。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m7_backup_restore_postgres.py tests/test_m6_backup_archive.py tests/test_m6_restore_postgres.py tests/test_m6_restore_safety.py -q
```

Expected: FAIL because archive accepts 0013/0015 and pre-cutover commit path still exists。

- [ ] **Step 3: 固定 M7 manifest and fail-before-mutation restore**

```python
ARCHIVE_SCHEMA_VERSION = 7
SUPPORTED_SCHEMA_REVISION = M7_FINAL_SCHEMA_REVISION


def require_supported_archive(manifest: ArchiveManifest) -> None:
    if (
        manifest.archive_schema_version != ARCHIVE_SCHEMA_VERSION
        or manifest.schema_revision != SUPPORTED_SCHEMA_REVISION
    ):
        raise UnsupportedArchiveSchema("UNSUPPORTED_ARCHIVE_SCHEMA")
```

Call this immediately after authentication and before target name resolution/creation。Verify the authenticated schema digest against the canonical M7 baseline digest before `pg_restore`, then verify the restored schema again before probes/proof。Update AAD、manifest JSON schema、proof payload and CLI result to carry version and digest。Delete all `PRE_M6_SCHEMA_REVISION`/commit-proof branches while preserving external authenticated encryption and secret separation。

- [ ] **Step 4: 运行 recovery gates and disposable drill**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m7_backup_restore_postgres.py tests/test_m6_backup_archive.py tests/test_m6_restore_postgres.py tests/test_m6_restore_safety.py tests/test_m6_retention_purge_postgres.py tests/test_m6_tombstone_journal.py -q
uv run pytest tests/blocking_io/test_m6_backup_subprocess.py tests/blocking_io/test_m6_restore_workflow.py -q
uvx ruff check app/recovery scripts/backup_postgres.py scripts/restore_postgres.py scripts/drill_restore.py
rg -n "PRE_M6|pre[_-]cutover|0013_project_automation_finalize|0015_project_reliability_finalize" app/recovery scripts/backup_postgres.py scripts/restore_postgres.py scripts/drill_restore.py
git add app/recovery scripts tests ../docs/operations/m6-backup-recovery.md ../AGENTS.md AGENTS.md
git commit -m "refactor: require M7 recovery archives"
```

Expected: recovery gates PASS with 0 PostgreSQL skips; final `rg` returns no matches。

### Task 10: 建立固定 M1–M7 release gate和source-absence gate

**Files:**

- Modify: `Makefile`
- Modify: `.github/workflows/project-foundation-postgres-tests.yml`
- Modify: `.github/workflows/backend-unit-tests.yml`
- Modify: `.github/workflows/frontend-unit-tests.yml`
- Create: `backend/tests/test_m7_source_absence.py`
- Create: `backend/tests/test_m7_release_gate_postgres.py`
- Create: `backend/tests/test_m7_process_boundary.py`
- Modify: `backend/tests/support/release_gate_plugin.py`
- Modify: `backend/tests/test_multi_worker_postgres_gate.py`
- Modify: `backend/tests/test_m6_gateway_reconnect_process.py`
- Modify: `backend/tests/test_m6_scheduler_process.py`
- Modify: `frontend/package.json`
- Modify: `frontend/playwright.config.ts`
- Modify: `frontend/playwright.static.config.ts`

**Interfaces:**

- Root `PROJECT_FOUNDATION_POSTGRES_TESTS` remains the only ordered fixed PostgreSQL list and is renamed/documented as M1–M7；it includes final M1–M6 capability tests plus M7 baseline/bootstrap/recovery/release boundary tests, not deleted migration tests。
- `test_m7_source_absence.py` walks only production roots and checks banned modules/routes/config keys/imports with a reviewed allowlist for tombstone validator and historical docs。
- Multi-process gate starts real Scheduler、Worker and Gateway against one random M7 database; it proves Gateway cannot execute Agent jobs, Scheduler cannot execute graph code, Worker owns leases, reconnect cursors survive Gateway restart and project/owner data never crosses scope。
- Missing `POSTGRES_TEST_URL` still fails before pytest collection and every selected database test must report 0 skips。

- [ ] **Step 1: 写 release/source/process RED tests**

```python
BANNED_PRODUCTION_PATHS = (
    "app/gateway/routers/threads.py",
    "app/gateway/routers/scheduled_tasks.py",
    "app/automations/legacy_reads.py",
    "packages/harness/deerflow/config/extensions_config.py",
    "packages/harness/deerflow/runtime/stream_bridge",
)


def test_deleted_production_paths_do_not_exist(repo_root):
    for relative in BANNED_PRODUCTION_PATHS:
        assert not (repo_root / "backend" / relative).exists()
```

The process child records PID/role for admission、claim、graph execution and terminal append; test asserts one Worker PID performs graph execution and no Gateway/Scheduler PID does。

- [ ] **Step 2: 运行 RED tests and gate-list validation**

```bash
cd backend
uv run pytest tests/test_m7_source_absence.py tests/test_m7_process_boundary.py -q
cd ..
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" make test-project-foundation-postgres
```

Expected: FAIL until old files are fully absent and Makefile/CI list is updated。

- [ ] **Step 3: 固定 ordered M1–M7 test list and CI**

Required coverage categories in the Makefile list: baseline/schema、account/project governance、shared asset/credential、private Thread/Run/file/artifact/memory/connection、Automation、job/Worker/Scheduler、durable stream/SSE reconnect、quota/audit、retention/backup/restore、multi-process boundary and M7 absence。CI checks list drift by importing the Makefile value, not duplicating a second order source。

- [ ] **Step 4: 运行 complete release gates**

```bash
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" make test-project-foundation-postgres
cd backend
uv run pytest -q
uv run pytest tests/blocking_io -q
uvx ruff format --check .
uvx ruff check .
cd ../frontend
pnpm test
pnpm check
pnpm exec playwright test tests/e2e/m7-project-only-routes.spec.ts tests/e2e/project-automations.spec.ts
pnpm exec playwright test --config playwright.static.config.ts
BUILD_MODE=production pnpm build
BUILD_MODE=static pnpm build
```

Expected: all commands PASS; PostgreSQL line reports 0 skipped。

- [ ] **Step 5: 提交 release gate**

```bash
cd ..
git add Makefile .github backend/tests frontend/package.json frontend/playwright.config.ts frontend/playwright.static.config.ts AGENTS.md backend/AGENTS.md frontend/AGENTS.md
git commit -m "test: establish M1-M7 release gate"
```

### Task 11: 同步文档、完成独立审查并关闭 M7

**Files:**

- Modify: `AGENTS.md`
- Modify: `backend/AGENTS.md`
- Modify: `frontend/AGENTS.md`
- Modify: `README.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-07-12-project-first-saas-design.md`
- Modify: `docs/superpowers/specs/2026-07-18-project-legacy-cleanup-m7-design.md`
- Modify: `docs/superpowers/plans/2026-07-18-project-legacy-cleanup-m7.md`
- Modify: `docs/operations/m6-backup-recovery.md`
- Delete: `docs/operations/m4-private-work-migration.md`
- Delete: `docs/operations/m5-automation-migration.md`
- Delete: `docs/operations/m6-reliability-migration.md`

**Acceptance:**

- Root docs describe only final setup/project/admin/Worker/Scheduler/recovery paths；active documentation contains no command for old data migration or global APIs。
- `test_m7_source_absence.py` validates every relative Markdown link in the active docs changed by M7 and ignores external URLs/anchors；no separate network link checker is required。
- Historical specs/plans remain in git as decision history, but the M7 spec status becomes `Completed` with evidence links and the master milestone ledger becomes 7/8（87.5%）。
- Independent review compares the full branch diff against the M7 spec and this plan；all Critical/Important findings are fixed and affected/full gates rerun before closure。
- M8 remains explicitly pending; completion text must not say the complete SaaS release is ready。

- [x] **Step 1: 更新 active docs and milestone ledger**

Document fresh install: create empty PostgreSQL DB → `make setup-db` → `make start`; document `M7_RECREATE_REQUIRED` as manual new-database action。Document system asset catalog seed, project-only URLs, process boundaries and M7-only restore。Remove obsolete root/module guidance and deleted make targets。

- [x] **Step 2: 运行 doc/source consistency checks**

```bash
rg -n "migrate-(sqlite|assets|private-work|automations|reliability)|/api/(threads|runs|assistants|memory|scheduled-tasks|agents|skills|mcp/config)|extensions_config|CUTOVER" README.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md docs/operations
rg -n "M7|7/8|87.5%|M8" AGENTS.md docs/superpowers/specs/2026-07-12-project-first-saas-design.md docs/superpowers/specs/2026-07-18-project-legacy-cleanup-m7-design.md CHANGELOG.md
```

Expected: first command returns no obsolete active-doc matches; second returns the updated ledger and M8 warning。

- [x] **Step 3: 运行 final clean-tree verification from the Task 10 gate list**

```bash
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" make test-project-foundation-postgres
cd backend
uv run pytest -q
uv run pytest tests/blocking_io -q
uvx ruff format --check .
uvx ruff check .
cd ../frontend
pnpm test
pnpm check
BUILD_MODE=production pnpm build
BUILD_MODE=static pnpm build
cd ..
make doctor
make check-db
make help
cd backend
uv run python ../scripts/support_bundle.py --out /tmp/deerflow-m7-support-bundle.zip
cd ..
git diff --check
git status --short
```

Expected: all gates PASS、0 PostgreSQL skips、support bundle contains no secret/removed extension field、no local Markdown link is broken，and only intended documentation/status changes are uncommitted before the closure commit。After process tests, verify no child PID/listener/database from the random M7 fixtures remains。

- [x] **Step 4: 请求 independent code review and repair findings**

Use `superpowers:requesting-code-review` with review range `00f7ae3c..HEAD` and require explicit review of: spec coverage、deleted-surface absence、project/owner authority、system-admin content redaction、Gateway/Worker/Scheduler boundaries、baseline schema equality、old-DB fail-before-DDL、M7 recovery invariants、frontend static no-network contract。Record every Task 1–10 review range plus the final range/verdict in the M7 spec；Minor findings must be recorded with evidence that they do not block M8。

For every Critical/Important finding, use `superpowers:receiving-code-review`, reproduce with a failing test, fix, rerun the affected gate and then rerun the Task 10 full release gate。Do not close M7 on implementation green alone。

- [x] **Step 5: 提交 closure docs**

```bash
git add AGENTS.md backend/AGENTS.md frontend/AGENTS.md README.md CHANGELOG.md docs
git commit -m "docs: close M7 legacy cleanup"
git status --short
git log --oneline --decorate -12
```

Expected: clean working tree; branch contains task commits, independent-review repairs if any and final closure commit。Report branch、HEAD、review range、gate commands/results and remaining M8 status to the user。

---

## Plan Self-review Checklist

- [x] Every M7 design-spec removal domain maps to at least one implementation task and one negative test。
- [x] No placeholder (`TODO`, `TBD`, `... implement later`) remains in executable examples or acceptance criteria。
- [x] Public names are consistent: `M7_FINAL_SCHEMA_REVISION`、`schema_ready`、`schema_state`、`AutomationSchedulerService`、`ProjectPrivateWorkScope`。
- [x] Temporary `PRE_RESET_SCHEMA_REVISION` exists only through Tasks 1–7 and is removed in Task 8。
- [x] Final database tests compare baseline DDL with runtime metadata plus manually owned functions/triggers, not table names alone。
- [x] Old database and pre-M7 archive failure tests assert no mutation before failure。
- [x] Every deleted route has backend `404` or frontend not-found coverage and no redirect/`410` compatibility branch。
- [x] Every retained private operation proves `project_id + owner_user_id` isolation and server-owned authority fields。
- [x] Full release acceptance includes real PostgreSQL 0-skip、backend unit/blocking-I/O、frontend unit/E2E/static/live builds and independent review。
- [x] Closure states 7/8（87.5%）and leaves M8 pending。
