# M5 Project Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有 legacy scheduled-task MVP 迁为 project+owner scoped Automation，并通过持久化 occurrence、权限重校验和 M4 private-run admission 提供可恢复、可隔离的项目自动化。

**Architecture:** 保留 Gateway 内嵌 Scheduler，但把 definition、occurrence、claim/lease 和 cutover authority 全部放入 PostgreSQL。Project API 和 Scheduler 都从 server-issued `PrivateWorkContext` 出发；automatic/manual trigger 先持久化 occurrence，再创建或复用 project-private Thread，最后调用唯一的 `start_private_run()` 链。M5 不建设通用 jobs、独立 Worker、跨 Worker SSE 或 admission 后自动重放。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy async、Alembic、PostgreSQL、pytest/pytest-asyncio、Next.js 16、React 19、TypeScript 5.8、TanStack Query 5、Zod、Rstest、Playwright、pnpm 10.26.2。

## Global Constraints

- PostgreSQL 是唯一 authority；M5 不增加 SQLite、memory persistence、Redis、Kafka 或外部队列后端。
- Automation 必须同时按 `project_id + owner_user_id` 隔离；禁止先按裸 ID 查询再在应用内判断 scope。
- Admin、Editor、Runner 通过 `automation.manage_own` 管理自己的 Automation；Viewer 只读自己的 definition/history。
- Trigger 时必须重新要求 `automation.manage_own`、`private_work.create`、`shared_assets.execute`。
- 客户端不能提供可信 project、owner、membership、role、capability、asset version、credential grant 或 `non_interactive`。
- Automatic/manual trigger 必须复用 M4 `start_private_run()`；禁止第二套 Agent executor。
- M5 只支持 `once` 与五字段 `cron`、IANA timezone、固定 overlap=`skip`、固定 misfire coalesce。
- Run admission 后的进程崩溃只标记 `interrupted`，不得自动重放可能已有副作用的 Agent run。
- `scheduler.enabled=false` 只停止 automatic poll；manual trigger 在 Gateway runtime ready 时仍可用。
- M5 受支持运行拓扑为单 Gateway；多 Gateway runtime ownership、通用 jobs/attempts/dead jobs、Worker lease 和持久化 SSE 属于 M6。
- Membership revoke、Viewer downgrade、project suspend/pending deletion 必须冻结 definition、取消 queued occurrence，并沿用 M4 cancellation marker 终止 active run。
- Migration 必须 maintenance-window、dry-run first、显式 owner/project/Agent map、幂等 ledger、fail-before-DDL finalize 和 singleton cutover marker。
- Prompt/title/run output 是私有内容，不得进入普通日志、迁移摘要、治理审计、URL query 或 localStorage。
- Backend 和 Frontend 均按 TDD 实施；每个任务先证明目标测试失败，再写最小实现，再验证并单独提交。
- 真实 PostgreSQL 测试只能创建随机 `deerflow_test_*` 数据库；CI 缺 `POSTGRES_TEST_URL` 必须在 pytest 前硬失败。
- 产品入口只能在 final schema、M4 marker、M5 marker、readiness、compile-time flag 和 server capability 同时通过后开放。
- 现有用户改动不得覆盖；实施前创建 `codex/m5-project-automation` 隔离分支或 worktree。

---

## File Structure

### Backend persistence

- `backend/packages/harness/deerflow/persistence/scheduled_tasks/model.py` — final definition ORM。
- `backend/packages/harness/deerflow/persistence/scheduled_tasks/sql.py` — session-bound scoped definition repository。
- `backend/packages/harness/deerflow/persistence/scheduled_task_runs/model.py` — durable occurrence ORM。
- `backend/packages/harness/deerflow/persistence/scheduled_task_runs/sql.py` — scoped occurrence/history/claim repository。
- `backend/packages/harness/deerflow/persistence/automations/model.py` — migration run、ledger、cutover singleton。
- `backend/packages/harness/deerflow/persistence/revisions.py` — startup-cached Alembic ancestry，供 M4/M5 guard 使用。
- `backend/packages/harness/deerflow/persistence/migrations/versions/0012_project_automation_expand.py` — nullable expand/control tables。
- `backend/packages/harness/deerflow/persistence/migrations/versions/0013_project_automation_finalize.py` — probe-first rename/NOT NULL/composite constraints。

### Backend application

- `backend/app/automations/models.py` — app-layer immutable commands/read models。
- `backend/app/automations/errors.py`、`error_mapping.py` — stable public errors。
- `backend/app/automations/cutover.py`、`readiness.py` — M4+M5 schema/marker gates。
- `backend/app/automations/service.py` — definition CRUD/state transitions。
- `backend/app/automations/occurrences.py` — scheduled/manual reservation、global admission lock、claim/lease。
- `backend/app/automations/dispatcher.py` — context re-resolution、Thread preparation、private run launch。
- `backend/app/automations/reconciliation.py` — completion CAS 与 restart reconciliation。
- `backend/app/scheduler/service.py` — lifecycle/poll loop，仅委托 occurrence/dispatcher。

### Gateway and lifecycle

- `backend/app/gateway/routers/project_automations.py` — strict project HTTP contract。
- `backend/app/gateway/routers/scheduled_tasks.py` — legacy guard；不再是 authority。
- `backend/app/gateway/services.py` — deterministic internal run ID 与 scheduler-only context injection。
- `backend/app/gateway/deps.py`、`app.py` — service wiring、completion hook、Scheduler lifecycle。
- `backend/app/private_work/retention.py` — freeze/restore Automation。
- `backend/app/projects/membership_service.py`、`lifecycle_service.py` — 保持现有事务锁序，复用扩展后的 retention hook。

### Migration and operations

- `backend/scripts/migrate_automations.py` — dry-run/execute、map validation、ledger、cutover。
- `backend/scripts/check_postgres.py`、`backend/scripts/setup_postgres.py`、`scripts/doctor.py` — head/table/readiness checks。
- `Makefile`、`backend/Makefile` — `migrate-automations` target。
- `docs/operations/m5-automation-migration.md` — maintenance-window runbook。

### Frontend

- `frontend/src/core/project-automations/{types,query-keys,api,hooks,readiness}.ts` — strict project client/cache layer。
- `frontend/src/components/projects/automations/{project-automations-page,automation-form,automation-workbench}.tsx` — project UI。
- `frontend/src/app/projects/[project_slug]/automations/page.tsx` — route wrapper。
- `frontend/src/components/projects/project-nav.tsx`、`project-shell.tsx` — gated navigation。
- `frontend/src/components/workspace/thread-scheduled-tasks-link.tsx`、`chats/scoped-chat-page.tsx`、`frontend/src/components/projects/private-work/project-chat-page.tsx` — project Thread link，不回退 legacy。
- `frontend/src/core/projects/features.ts` — `PROJECT_AUTOMATION` build flag。

## Gate and Dependency Order

1. **Gate 1 — schema、scope、cutover foundation:** Tasks 1–3。
2. **Gate 2 — definition 与 durable dispatch:** Tasks 4–8。
3. **Gate 3 — HTTP、migration、Frontend:** Tasks 9–14。
4. **Gate 4 — PostgreSQL/Frontend release gate 与文档:** Tasks 15–18。

`PROJECT_AUTOMATION` 只能在 Task 17 的隔离门禁通过后设置为 `true as const`；M5 状态只能在 Task 18 的 fresh full verification 和独立审查结束后改为已完成。

---

### Task 1: 建立 M5 final ORM、staged revisions 和 revision ancestry

**Files:**

- Create: `backend/packages/harness/deerflow/persistence/automations/__init__.py`
- Create: `backend/packages/harness/deerflow/persistence/automations/model.py`
- Create: `backend/packages/harness/deerflow/persistence/revisions.py`
- Modify: `backend/packages/harness/deerflow/persistence/scheduled_tasks/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/scheduled_task_runs/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/models/__init__.py`
- Modify: `backend/packages/harness/deerflow/persistence/bootstrap.py`
- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/0012_project_automation_expand.py`
- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/0013_project_automation_finalize.py`
- Modify: `backend/app/private_work/cutover.py`
- Create: `backend/tests/test_m5_automation_schema_postgres.py`
- Modify: `backend/tests/test_persistence_migrations_env.py`
- Modify: `backend/tests/test_default_project_bootstrap.py`
- Modify: `backend/tests/test_private_work_cutover_guard.py`

**Interfaces:**

- Produces ORM rows `ScheduledTaskRow`、`ScheduledTaskRunRow`、`AutomationMigrationRunRow`、`AutomationMigrationLedgerRow`、`AutomationCutoverStateRow`。
- Produces `RevisionAncestry.from_script_directory() -> RevisionAncestry` and `contains(current: str, required: str) -> bool`，module import/startup后只读内存 map。
- `0012` down revision固定 `0011_private_artifact_tombstone`；`0013` down revision固定 `0012_project_automation_expand`。
- `0013` 在任何 rename/NOT NULL/FK DDL 前调用 `_assert_finalize_ready(connection)`。

- [ ] **Step 1: 写 final schema、staged migration 与 descendant revision 失败测试**

```python
M5_TABLES = {
    "scheduled_tasks",
    "scheduled_task_runs",
    "automation_migration_runs",
    "automation_migration_ledger",
    "automation_cutover_state",
}


async def test_m5_final_schema_has_private_scope_and_occurrence_constraints(
    migrated_postgres_database_url,
):
    engine = create_async_engine(migrated_postgres_database_url)
    async with engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
        task_columns = await connection.run_sync(
            lambda sync: {c["name"]: c for c in inspect(sync).get_columns("scheduled_tasks")}
        )
        run_columns = await connection.run_sync(
            lambda sync: {c["name"]: c for c in inspect(sync).get_columns("scheduled_task_runs")}
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert M5_TABLES <= tables
    assert task_columns["project_id"]["nullable"] is False
    assert task_columns["owner_user_id"]["nullable"] is False
    assert "user_id" not in task_columns
    assert run_columns["occurrence_key"]["nullable"] is False
    assert run_columns["project_id"]["nullable"] is False
    assert revision == "0013_project_automation_finalize"
    await engine.dispose()


def test_revision_ancestry_accepts_m5_as_m4_descendant():
    ancestry = RevisionAncestry.from_script_directory()
    assert ancestry.contains("0013_project_automation_finalize", "0011_private_artifact_tombstone")
    assert ancestry.contains("0013_project_automation_finalize", "0013_project_automation_finalize")
    assert not ancestry.contains("0011_private_artifact_tombstone", "0013_project_automation_finalize")
```

同时在同一测试文件明确断言：

```python
EXPECTED_TASK_CHECKS = {
    "ck_scheduled_tasks_context_mode",
    "ck_scheduled_tasks_schedule_type",
    "ck_scheduled_tasks_status",
    "ck_scheduled_tasks_overlap_policy",
    "ck_scheduled_tasks_thread_mode",
    "ck_scheduled_tasks_agent_scope",
    "ck_scheduled_tasks_version",
}

EXPECTED_RUN_CHECKS = {
    "ck_scheduled_task_runs_trigger",
    "ck_scheduled_task_runs_status",
    "ck_scheduled_task_runs_run_requires_thread",
    "ck_scheduled_task_runs_attempt_count",
}
```

并覆盖 task→membership/thread/Agent、occurrence→task/thread/run 的 composite FK、manual idempotency partial unique、active occurrence index、singleton marker CHECK、fresh `Base.metadata.create_all` 与 migration head shape一致。

- [ ] **Step 2: 运行 schema tests，确认当前 legacy shape 失败**

Run:

```bash
cd backend
uv run pytest tests/test_m5_automation_schema_postgres.py tests/test_persistence_migrations_env.py tests/test_default_project_bootstrap.py tests/test_private_work_cutover_guard.py -q
```

Expected: FAIL，原因包含缺少 `project_id`、`automation_cutover_state`、revision仍为 `0011_private_artifact_tombstone` 或 M4 guard拒绝 descendant head。真实 PostgreSQL fixture若 skip，先配置测试数据库，不能把 skip 当作失败证据。

- [ ] **Step 3: 把 definition 和 occurrence ORM 改为 final shape**

`ScheduledTaskRow` 的 authority fields按以下定义实现；保留现有 string primary key，只保留受约束的 last outcome summary：

```python
class ScheduledTaskRow(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    agent_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule_spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="enabled")
    overlap_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="skip")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outcome: Mapped[str | None] = mapped_column(String(24))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    run_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

`ScheduledTaskRunRow` 使用 durable occurrence fields：

```python
class ScheduledTaskRunRow(Base):
    __tablename__ = "scheduled_task_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurrence_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    manual_idempotency_hash: Mapped[str | None] = mapped_column(CHAR(64))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    resolved_membership_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    resolved_membership_version: Mapped[int | None] = mapped_column(BigInteger)
    launch_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

安装规格第 7 节列出的完整 CHECK、composite FK、unique 和 partial indexes；删除 unscoped `user_id` synonym、`last_run_id`、`last_thread_id` 和 task-row lease fields。

- [ ] **Step 4: 建立 migration control rows 和 revision ancestry cache**

`automations/model.py` 使用不可变 ledger：

```python
class AutomationCutoverStateRow(Base):
    __tablename__ = "automation_cutover_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("automation_migration_runs.id", ondelete="RESTRICT")
    )
    empty_domain_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    final_schema_probe_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

`RevisionAncestry` 一次读取 Alembic script tree并存 ancestor closure：

```python
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _script_directory() -> ScriptDirectory:
    config = AlembicConfig()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config)


@dataclass(frozen=True, slots=True)
class RevisionAncestry:
    ancestors: Mapping[str, frozenset[str]]

    @classmethod
    def from_script_directory(cls) -> "RevisionAncestry":
        script = _script_directory()
        mapping: dict[str, frozenset[str]] = {}
        for revision in script.walk_revisions(base="base", head="heads"):
            lineage = {revision.revision}
            parent = revision.down_revision
            while isinstance(parent, str):
                lineage.add(parent)
                parent = script.get_revision(parent).down_revision
            mapping[revision.revision] = frozenset(lineage)
        return cls(mapping)

    def contains(self, current: str, required: str) -> bool:
        return required in self.ancestors.get(current, frozenset())
```

缓存由 Gateway startup建立并传给 M4/M5 guards；request path只查 map，未知 revision/branch返回 false。

- [ ] **Step 5: 编写 expand 与 fail-before-DDL finalize**

`0012` 只做 additive/nullable 变化、control tables和supporting indexes：

```python
revision = "0012_project_automation_expand"
down_revision = "0011_private_artifact_tombstone"


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("owner_user_id", sa.String(36), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("agent_asset_id", sa.Uuid(), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("agent_scope", sa.String(16), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("version", sa.BigInteger(), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("last_outcome", sa.String(24), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("last_error_code", sa.String(64), nullable=True))
    _add_occurrence_expand_columns()
    _create_automation_migration_control_tables()
```

`0013` 第一条业务操作必须是 probe：

```python
def upgrade() -> None:
    connection = op.get_bind()
    _assert_finalize_ready(connection)
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_column("last_run_id")
        batch.drop_column("last_thread_id")
        batch.drop_column("last_error")
        batch.drop_column("lease_owner")
        batch.drop_column("lease_expires_at")
        batch.drop_column("assistant_id")
        batch.drop_column("user_id")
        batch.alter_column("project_id", nullable=False)
        batch.alter_column("owner_user_id", nullable=False)
        batch.alter_column("agent_asset_id", nullable=False)
        batch.alter_column("agent_scope", nullable=False)
        batch.alter_column("version", nullable=False)
    _install_final_task_constraints()
    _install_final_occurrence_constraints()
```

`_assert_finalize_ready` 只接受两个互斥分支：

```python
def _assert_finalize_ready(connection: Connection) -> None:
    if _is_empty_automation_domain(connection):
        _assert_m4_cutover_complete(connection)
        _record_empty_domain_probe(connection)
        return
    _assert_marker_stage(connection, "migration_ready")
    _assert_domain_ledgers_complete(connection)
    _assert_source_target_counts(connection)
    _assert_scope_agent_thread_run_relations(connection)
```

非空分支要求两 domain ledger complete、source/target row count一致、所有 scope/Agent/thread/run关系可建立；
空分支要求 task/run 均为零且 M4 marker complete。任一失败必须发生在任何 destructive DDL前。

- [ ] **Step 6: 更新 fresh bootstrap 和 M4 cutover guard**

Fresh DB通过final ORM `create_all`后stamp head，或通过线性Alembic执行上述empty-domain finalize分支；两条路径都必须在
legacy automation domain为空且 M4 marker complete时完成空域probe，final schema probe通过后才写：

```sql
INSERT INTO automation_cutover_state
    (id, stage, empty_domain_probe_complete, final_schema_probe_complete, cutover_at, updated_at)
VALUES
    (1, 'cutover_complete', true, true, now(), now())
ON CONFLICT (id) DO NOTHING
```

Existing `0011` database若 scheduled tables非空，ordinary startup报 `automation migration required`，不得自动应用 `0012/0013`。M4 guard改为：

```python
if not marker.cutover_complete or not self._revisions.contains(
    current_revision,
    PRIVATE_WORK_REQUIRED_REVISION,
):
    raise PrivateWorkCutover(self.request_id)
```

- [ ] **Step 7: 运行 schema tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_m5_automation_schema_postgres.py tests/test_persistence_migrations_env.py tests/test_default_project_bootstrap.py tests/test_private_work_cutover_guard.py -q
```

Expected: PASS；真实 PostgreSQL项目不得 skip。

```bash
git add backend/packages/harness/deerflow/persistence backend/app/private_work/cutover.py backend/tests/test_m5_automation_schema_postgres.py backend/tests/test_persistence_migrations_env.py backend/tests/test_default_project_bootstrap.py backend/tests/test_private_work_cutover_guard.py
git commit -m "feat: add M5 automation schema foundation"
```

---

### Task 2: 建立强制 scope 的 definition 与 occurrence repositories

**Files:**

- Modify: `backend/packages/harness/deerflow/persistence/scheduled_tasks/__init__.py`
- Modify: `backend/packages/harness/deerflow/persistence/scheduled_tasks/sql.py`
- Modify: `backend/packages/harness/deerflow/persistence/scheduled_task_runs/__init__.py`
- Modify: `backend/packages/harness/deerflow/persistence/scheduled_task_runs/sql.py`
- Create: `backend/tests/test_project_automation_repository.py`
- Modify: `backend/tests/test_scheduled_task_repository.py`

**Interfaces:**

- Produces `ScheduledTaskCreate`、`ScheduledTaskRecord`、`ScheduledTaskPatch`。
- Produces `ScheduledTaskRunCreate`、`ScheduledTaskRunRecord`。
- `ScheduledTaskRepository(session: AsyncSession)` 和 `ScheduledTaskRunRepository(session: AsyncSession)` 都是 session-bound；每个方法第一个 authority 参数是 exact `PrivateResourceScope`。
- 后续 Task 4–8 只消费这些 repository；legacy router不得直接实例化或传裸 owner。

- [ ] **Step 1: 写跨 project/owner CRUD、history 和裸 ID 禁止测试**

```python
async def test_task_repository_never_returns_cross_owner(seed):
    async with seed.factory() as session, session.begin():
        owner_repo = ScheduledTaskRepository(session)
        created = await owner_repo.create(
            seed.owner_scope,
            ScheduledTaskCreate(
                task_id="task-owner",
                thread_id=None,
                context_mode="fresh_thread_per_run",
                agent_asset_id=seed.agent_id,
                agent_scope="system",
                title="Owner task",
                prompt="private prompt",
                schedule_type="cron",
                schedule_spec={"cron": "0 9 * * *"},
                timezone="Asia/Shanghai",
                next_run_at=seed.now,
            ),
        )
    async with seed.factory() as session:
        outsider_repo = ScheduledTaskRepository(session)
        assert await outsider_repo.get(seed.outsider_scope, created.id) is None
        assert await outsider_repo.list(seed.outsider_scope, limit=50, offset=0) == ()


def test_repository_requires_private_resource_scope():
    with pytest.raises(TypeError):
        ScheduledTaskRepository.predicates({"project_id": "forged"})
```

Occurrence relation test：

```python
async def test_occurrence_history_is_scoped_by_parent(seed):
    async with seed.factory() as session, session.begin():
        repo = ScheduledTaskRunRepository(session)
        await repo.create(
            seed.owner_scope,
            ScheduledTaskRunCreate(
                occurrence_id="task-run-owner",
                task_id="task-owner",
                task_version=1,
                occurrence_key="a" * 64,
                manual_idempotency_hash=None,
                scheduled_for=seed.now,
                trigger="scheduled",
                status="queued",
            ),
        )
    async with seed.factory() as session:
        repo = ScheduledTaskRunRepository(session)
        assert await repo.list_by_task(seed.outsider_scope, "task-owner", limit=50, offset=0) == ()
```

- [ ] **Step 2: 运行 repository tests，确认 legacy factory/user-id API 失败**

Run:

```bash
cd backend
uv run pytest tests/test_project_automation_repository.py tests/test_scheduled_task_repository.py -q
```

Expected: FAIL，原因包含缺少 typed commands、repository constructor仍接收 session factory或方法仍暴露 `user_id`。

- [ ] **Step 3: 定义 immutable commands/records 和 scope predicate**

在 `scheduled_tasks/sql.py` 定义：

```python
@dataclass(frozen=True, slots=True)
class ScheduledTaskCreate:
    task_id: str
    thread_id: str | None
    context_mode: str
    agent_asset_id: uuid.UUID
    agent_scope: str
    title: str
    prompt: str
    schedule_type: str
    schedule_spec: dict[str, object]
    timezone: str
    next_run_at: datetime | None


@dataclass(frozen=True, slots=True)
class ScheduledTaskPatch:
    title: str | None = None
    prompt: str | None = None
    schedule_spec: dict[str, object] | None = None
    timezone: str | None = None
    next_run_at: datetime | None = None
    status: str | None = None


class ScheduledTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise TypeError("PrivateResourceScope is required")
        return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))

    @classmethod
    def predicates(cls, scope: PrivateResourceScope):
        project_id, owner_user_id = cls.coordinates(scope)
        return (
            ScheduledTaskRow.project_id == project_id,
            ScheduledTaskRow.owner_user_id == owner_user_id,
        )
```

所有 get/list/update/delete query必须直接包含 `*predicates(scope)`；禁止 `session.get(ScheduledTaskRow, task_id)` 后再判断 owner。

- [ ] **Step 4: 实现 definition CRUD、version CAS 和 queued cancellation**

Exact mutation contract：

```python
async def update(
    self,
    scope: PrivateResourceScope,
    task_id: str,
    *,
    expected_version: int,
    values: Mapping[str, object],
) -> ScheduledTaskRecord | None:
    statement = (
        sa.update(ScheduledTaskRow)
        .where(
            ScheduledTaskRow.id == task_id,
            ScheduledTaskRow.version == expected_version,
            ScheduledTaskRow.deleted_at.is_(None),
            *self.predicates(scope),
        )
        .values(**dict(values), version=ScheduledTaskRow.version + 1, updated_at=datetime.now(UTC))
        .returning(ScheduledTaskRow)
    )
    row = (await self.session.execute(statement)).scalar_one_or_none()
    return None if row is None else self.record(row)
```

提供 `lock_active()`、`list()`、`list_by_thread()`、`soft_delete()`；每个 list都有 hard limit `1..1000` 和 deterministic `(created_at desc, id desc)`。

- [ ] **Step 5: 实现 scoped occurrence history 与 terminal CAS**

```python
TERMINAL_OCCURRENCE_STATUSES = frozenset(
    {"success", "failed", "skipped", "interrupted", "cancelled", "rejected"}
)


async def finish(
    self,
    scope: PrivateResourceScope,
    occurrence_id: str,
    *,
    status: str,
    error_code: str | None,
    error_message: str | None,
    finished_at: datetime,
) -> bool:
    result = await self.session.execute(
        sa.update(ScheduledTaskRunRow)
        .where(
            ScheduledTaskRunRow.id == occurrence_id,
            ScheduledTaskRunRow.status.not_in(TERMINAL_OCCURRENCE_STATUSES),
            *self.predicates(scope),
        )
        .values(
            status=status,
            error_code=error_code,
            error_message=error_message,
            finished_at=finished_at,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=finished_at,
        )
    )
    return result.rowcount == 1
```

提供 scoped `get()`、`get_by_agent_run_id()`、`list_by_task()`、`has_active()`、`cancel_queued()`，并保证 output record不暴露 lease/idempotency hash给 HTTP层。

- [ ] **Step 6: 运行 repository tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_project_automation_repository.py tests/test_scheduled_task_repository.py -q
```

Expected: PASS。

```bash
git add backend/packages/harness/deerflow/persistence/scheduled_tasks backend/packages/harness/deerflow/persistence/scheduled_task_runs backend/tests/test_project_automation_repository.py backend/tests/test_scheduled_task_repository.py
git commit -m "feat: scope automation repositories"
```

---

### Task 3: 建立 Automation errors、cutover 和 readiness

**Files:**

- Create: `backend/app/automations/__init__.py`
- Create: `backend/app/automations/models.py`
- Create: `backend/app/automations/errors.py`
- Create: `backend/app/automations/error_mapping.py`
- Create: `backend/app/automations/cutover.py`
- Create: `backend/app/automations/readiness.py`
- Create: `backend/tests/test_automation_errors.py`
- Create: `backend/tests/test_automation_cutover.py`
- Create: `backend/tests/test_automation_readiness.py`

**Interfaces:**

- Produces `AutomationError` subclasses with stable code/status mapping。
- Produces `AutomationCutoverGuard.require_project_open()`、`require_legacy_open()`。
- Produces `AutomationReadinessService.read(session, context, scheduler_enabled) -> AutomationReadiness`。
- Produces immutable `AutomationCreate`、`AutomationChanges`、`AutomationView`、`AutomationRunView`。

- [ ] **Step 1: 写 error、marker/revision 和 readiness matrix 失败测试**

```python
@pytest.mark.parametrize(
    (error, status_code, code),
    [
        (AutomationNotFound("req"), 404, "AUTOMATION_NOT_FOUND"),
        (AutomationForbidden("req"), 403, "AUTOMATION_FORBIDDEN"),
        (AutomationVersionConflict("req"), 409, "AUTOMATION_VERSION_CONFLICT"),
        (AutomationActiveRun("req"), 409, "AUTOMATION_ACTIVE_RUN"),
        (AutomationCutover("req"), 409, "AUTOMATION_CUTOVER"),
        (AutomationConcurrencyLimit("req"), 429, "AUTOMATION_CONCURRENCY_LIMIT"),
        (AutomationUnavailable("req"), 503, "AUTOMATION_UNAVAILABLE"),
    ],
)
def test_automation_error_mapping(error, status_code, code):
    response = automation_http_exception(error)
    assert response.status_code == status_code
    assert response.detail == {
        "code": code,
        "message": error.public_message,
        "request_id": "req",
    }


async def test_project_guard_requires_m4_marker_m5_marker_and_descendant(seed):
    guard = AutomationCutoverGuard.for_session(seed.session, revisions=seed.revisions)
    await seed.set_private_marker("cutover_complete")
    await seed.set_automation_marker("migration_ready")
    with pytest.raises(AutomationCutover):
        await guard.require_project_open()
    await seed.set_automation_marker("cutover_complete")
    await guard.require_project_open()
```

Readiness test：

```python
async def test_readiness_reports_scheduler_disabled_without_closing_project_api(seed):
    result = await AutomationReadinessService(seed.revisions).read(
        seed.session,
        seed.context,
        scheduler_enabled=False,
    )
    assert result.status == "ready"
    assert result.scheduler_enabled is False
    assert result.project_private_work_ready is True
    assert result.automation_cutover_ready is True
```

- [ ] **Step 2: 运行 tests，确认 modules 尚不存在**

Run:

```bash
cd backend
uv run pytest tests/test_automation_errors.py tests/test_automation_cutover.py tests/test_automation_readiness.py -q
```

Expected: collection FAIL，提示 `app.automations` 不存在。

- [ ] **Step 3: 定义 stable errors 和 immutable app models**

```python
class AutomationError(Exception):
    code = "AUTOMATION_UNAVAILABLE"
    public_message = "Automation is temporarily unavailable."

    def __init__(self, request_id: str) -> None:
        super().__init__(self.public_message)
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class AutomationCreate:
    title: str
    prompt: str
    context_mode: Literal["fresh_thread_per_run", "reuse_thread"]
    thread_id: str | None
    agent_asset_id: uuid.UUID
    agent_scope: Literal["project", "system"]
    schedule_type: Literal["once", "cron"]
    schedule_spec: Mapping[str, object]
    timezone: str


@dataclass(frozen=True, slots=True)
class AutomationChanges:
    expected_version: int
    title: str | None = None
    prompt: str | None = None
    schedule_spec: Mapping[str, object] | None = None
    timezone: str | None = None
```

`AutomationView`/`AutomationRunView` 只包含 public fields；不包含 owner、lease owner、idempotency hash、raw runtime kwargs或credential identifiers。

- [ ] **Step 4: 实现 cutover guards 与 readiness**

`AutomationCutoverGuard.require_project_open()` 同时验证：

```python
if not private_marker_complete:
    raise AutomationCutover(request_id)
if not automation_marker_complete:
    raise AutomationCutover(request_id)
if not revisions.contains(current_revision, "0013_project_automation_finalize"):
    raise AutomationCutover(request_id)
```

`require_legacy_open()` 在 M5 marker complete时抛 `AutomationCutover`。Readiness捕获 SQLAlchemy errors并返回：

```python
AutomationReadiness(
    status="ready",
    code="AUTOMATION_READY",
    scheduler_enabled=scheduler_enabled,
    project_private_work_ready=True,
    automation_cutover_ready=True,
    request_id=context.request_id,
)
```

Marker/revision incomplete返回 `migration_required`；DB失败返回 `unavailable`，不把 exception text放 response。

- [ ] **Step 5: 运行 tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_automation_errors.py tests/test_automation_cutover.py tests/test_automation_readiness.py -q
```

Expected: PASS。

```bash
git add backend/app/automations backend/tests/test_automation_errors.py backend/tests/test_automation_cutover.py backend/tests/test_automation_readiness.py
git commit -m "feat: add automation cutover contracts"
```

---

### Task 4: 实现 definition service、schedule validation 和乐观并发

**Files:**

- Create: `backend/app/automations/service.py`
- Modify: `backend/packages/harness/deerflow/scheduler/schedules.py`
- Create: `backend/tests/test_project_automation_service.py`
- Modify: `backend/tests/test_scheduled_task_schedules.py`

**Interfaces:**

- Produces `ProjectAutomationService(session_factory, clock)`。
- Methods: `create(context, command)`、`get(context, task_id)`、`list(context, limit, offset, thread_id)`、`update(context, task_id, changes)`、`pause(context, task_id, expected_version)`、`resume(...)`、`delete(...)`。
- Produces `next_scheduled_occurrence(schedule_type, schedule_spec, timezone, now, coalesce=True)`；cron结果严格晚于 `now`。

- [ ] **Step 1: 写 role、Thread/Agent、version、pause/resume 和 coalesce 失败测试**

```python
async def test_viewer_can_read_but_cannot_create(seed):
    service = ProjectAutomationService(seed.factory, clock=lambda: seed.now)
    assert await service.list(seed.viewer_context, limit=50, offset=0) == ()
    with pytest.raises(AutomationForbidden):
        await service.create(seed.viewer_context, seed.create_command())


async def test_reuse_thread_must_match_scope_and_agent(seed):
    service = ProjectAutomationService(seed.factory, clock=lambda: seed.now)
    command = seed.create_command(
        context_mode="reuse_thread",
        thread_id=seed.outsider_thread_id,
        agent_asset_id=seed.agent_id,
    )
    with pytest.raises(AutomationNotFound):
        await service.create(seed.owner_context, command)


async def test_update_cancels_queued_and_rejects_running(seed):
    service = ProjectAutomationService(seed.factory, clock=lambda: seed.now)
    task = await seed.create_task(status="enabled")
    await seed.create_occurrence(task, status="running")
    with pytest.raises(AutomationActiveRun):
        await service.update(
            seed.owner_context,
            task.id,
            AutomationChanges(expected_version=task.version, title="Changed"),
        )
```

Schedule test：

```python
def test_cron_misfire_coalesces_to_one_future_tick():
    now = datetime(2026, 7, 16, 10, 30, tzinfo=UTC)
    result = next_scheduled_occurrence(
        "cron",
        {"cron": "0 * * * *"},
        "UTC",
        now=now,
        coalesce=True,
    )
    assert result == datetime(2026, 7, 16, 11, 0, tzinfo=UTC)
```

- [ ] **Step 2: 运行 tests，确认 service 不存在/legacy schedule语义不足**

Run:

```bash
cd backend
uv run pytest tests/test_project_automation_service.py tests/test_scheduled_task_schedules.py -q
```

Expected: FAIL，提示缺少 `ProjectAutomationService` 或 mutation仍未检查 scope/version。

- [ ] **Step 3: 实现 create/read/list 和 execution capability validation**

Create transaction固定锁序：project→membership→Thread/Agent→task。

```python
async def create(
    self,
    context: PrivateWorkContext,
    command: AutomationCreate,
) -> AutomationView:
    context = require_issued_private_work_context(context)
    async with self._session_factory() as session, session.begin():
        current = await self._revalidator.require(
            session,
            context,
            Capability.AUTOMATION_MANAGE_OWN,
            Capability.PRIVATE_WORK_CREATE,
            Capability.SHARED_ASSETS_EXECUTE,
            lock=True,
        )
        agent = await self._validate_target(session, context, current, command)
        next_run_at = self._validate_schedule(command, now=self._clock())
        record = await ScheduledTaskRepository(session).create(
            context.resource_scope,
            self._to_create(command, agent, next_run_at),
        )
    return self._view(record)
```

Read/list仅要求 `PRIVATE_WORK_READ_OWN`。`_validate_target` 对 reuse Thread进行同 scope active lookup并要求 Thread Agent与command完全相等；fresh mode要求 thread_id is None并解析Agent executable binding。

- [ ] **Step 4: 实现 update/pause/resume/delete state machine**

Mutation transaction：lock task→reject launching/running→cancel queued→CAS update。

```python
async def pause(
    self,
    context: PrivateWorkContext,
    task_id: str,
    expected_version: int,
) -> AutomationView:
    async with self._session_factory() as session, session.begin():
        await self._require_manage(session, context, lock=True)
        task = await self._lock_mutable(session, context, task_id, expected_version)
        await ScheduledTaskRunRepository(session).cancel_queued(
            context.resource_scope,
            task.id,
            now=self._clock(),
            error_code="AUTOMATION_PAUSED",
        )
        updated = await ScheduledTaskRepository(session).update(
            context.resource_scope,
            task.id,
            expected_version=expected_version,
            values={"status": "paused", "next_run_at": None},
        )
    if updated is None:
        raise AutomationVersionConflict(context.request_id)
    return self._view(updated)
```

Resume从当前时间计算 future tick；once `run_at <= now` 抛 `AutomationOnceExpired`。Delete soft-deletes并paused；history保留。

- [ ] **Step 5: 运行 service tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_project_automation_service.py tests/test_scheduled_task_schedules.py -q
```

Expected: PASS。

```bash
git add backend/app/automations/service.py backend/packages/harness/deerflow/scheduler/schedules.py backend/tests/test_project_automation_service.py backend/tests/test_scheduled_task_schedules.py
git commit -m "feat: add project automation lifecycle"
```

---

### Task 5: 实现 scheduled/manual occurrence reservation、global cap 和 claim lease

**Files:**

- Create: `backend/app/automations/occurrences.py`
- Modify: `backend/packages/harness/deerflow/persistence/scheduled_tasks/sql.py`
- Modify: `backend/packages/harness/deerflow/persistence/scheduled_task_runs/sql.py`
- Create: `backend/tests/test_automation_occurrences.py`
- Modify: `backend/tests/test_scheduled_task_claims.py`

**Interfaces:**

- Produces `AutomationOccurrenceService.reserve_due(now, limit) -> tuple[ScheduledTaskRunRecord, ...]`。
- Produces `reserve_manual(context, task_id, idempotency_key, now) -> ManualReservation`，重复 key返回原 occurrence。
- Produces `claim_next(now, lease_owner, lease_seconds) -> ScheduledTaskRunRecord | None`。
- PostgreSQL advisory xact lock key固定为 `_AUTOMATION_ADMISSION_LOCK = 0x0DEE_12F1_0A55_0005`。

- [ ] **Step 1: 写并发 tick、manual idempotency、overlap skip 和 global cap 失败测试**

```python
async def test_two_pollers_reserve_one_occurrence(postgres_seed):
    service_a = AutomationOccurrenceService(postgres_seed.factory, max_concurrent_runs=3)
    service_b = AutomationOccurrenceService(postgres_seed.factory, max_concurrent_runs=3)
    first, second = await asyncio.gather(
        service_a.reserve_due(now=postgres_seed.now, limit=10),
        service_b.reserve_due(now=postgres_seed.now, limit=10),
    )
    assert len(first) + len(second) == 1
    rows = await postgres_seed.occurrences()
    assert len(rows) == 1
    assert rows[0].occurrence_key == scheduled_occurrence_key(
        postgres_seed.task_id,
        postgres_seed.due_at,
    )


async def test_manual_idempotency_returns_same_occurrence(postgres_seed):
    service = AutomationOccurrenceService(postgres_seed.factory, max_concurrent_runs=3)
    key = uuid.UUID("11111111-1111-4111-8111-111111111111")
    first = await service.reserve_manual(
        postgres_seed.context,
        postgres_seed.task_id,
        key,
        now=postgres_seed.now,
    )
    second = await service.reserve_manual(
        postgres_seed.context,
        postgres_seed.task_id,
        key,
        now=postgres_seed.now,
    )
    assert second.occurrence.id == first.occurrence.id
    assert second.created is False
```

Global cap：

```python
async def test_manual_and_scheduled_share_global_cap(postgres_seed):
    await postgres_seed.create_occurrence(status="running")
    service = AutomationOccurrenceService(postgres_seed.factory, max_concurrent_runs=1)
    with pytest.raises(AutomationConcurrencyLimit):
        await service.reserve_manual(
            postgres_seed.context,
            postgres_seed.task_id,
            uuid.uuid4(),
            now=postgres_seed.now,
        )
```

- [ ] **Step 2: 运行 tests，确认 legacy task-row lease无法满足**

Run:

```bash
cd backend
uv run pytest tests/test_automation_occurrences.py tests/test_scheduled_task_claims.py -q
```

Expected: FAIL，原因包含缺少 occurrence service、manual idempotency、atomic next-run advance或global cap race。

- [ ] **Step 3: 实现 deterministic keys 和 atomic due reservation**

```python
def scheduled_occurrence_key(task_id: str, scheduled_for: datetime) -> str:
    canonical = f"scheduled:{task_id}:{scheduled_for.astimezone(UTC).isoformat()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def manual_occurrence_key(task_id: str, idempotency_hash: str) -> str:
    return hashlib.sha256(f"manual:{task_id}:{idempotency_hash}".encode("utf-8")).hexdigest()
```

`reserve_due` transaction：advisory lock→count `queued/launching/running`→`FOR UPDATE SKIP LOCKED` due tasks→insert occurrence→advance `next_run_at`。Cron advance调用 coalesce helper并保证结果 `> now`；once将 `next_run_at=None`。

如果同 task已有 launching/running occurrence，插入 terminal `skipped` history并设置 `AUTOMATION_OVERLAP_SKIPPED`，不占 cap。

- [ ] **Step 4: 实现 manual reservation 与 UUID header validation contract**

```python
@dataclass(frozen=True, slots=True)
class ManualReservation:
    occurrence: ScheduledTaskRunRecord
    created: bool


def hash_manual_idempotency(value: uuid.UUID) -> str:
    return hashlib.sha256(str(value).encode("ascii")).hexdigest()
```

Manual path在同一 advisory lock下先按 partial unique hash查询；存在即返回，active occurrence存在则抛 `AutomationActiveRun`，global cap耗尽抛 `AutomationConcurrencyLimit`。Manual occurrence不修改 task `next_run_at`。

- [ ] **Step 5: 实现 occurrence claim/lease**

Claim query只接受 `queued`、`next_attempt_at is null or <= now`，使用 `FOR UPDATE SKIP LOCKED`，写入：

```python
{
    "status": "launching",
    "lease_owner": lease_owner,
    "lease_expires_at": now + timedelta(seconds=lease_seconds),
    "launch_attempt_count": ScheduledTaskRunRow.launch_attempt_count + 1,
    "thread_id": deterministic_thread_id(occurrence),
    "run_id": deterministic_run_id(occurrence),
    "started_at": func.coalesce(ScheduledTaskRunRow.started_at, now),
    "updated_at": now,
}
```

Fresh Thread ID使用 UUIDv5 namespace `8bc2f65e-f186-5fb2-a480-7f23125f8005`；run ID使用 `a58150d1-9869-55b1-8cbe-cd30e6edba05`。Reuse mode保留 task thread ID。

- [ ] **Step 6: 运行 occurrence tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_automation_occurrences.py tests/test_scheduled_task_claims.py -q
```

Expected: PASS；PostgreSQL并发测试不得 skip。

```bash
git add backend/app/automations/occurrences.py backend/packages/harness/deerflow/persistence/scheduled_tasks/sql.py backend/packages/harness/deerflow/persistence/scheduled_task_runs/sql.py backend/tests/test_automation_occurrences.py backend/tests/test_scheduled_task_claims.py
git commit -m "feat: persist automation occurrences"
```

---

### Task 6: 通过 M4 private Thread/run 链启动 Automation

**Files:**

- Create: `backend/app/automations/dispatcher.py`
- Modify: `backend/app/private_work/context.py`
- Modify: `backend/app/private_work/run_admission.py`
- Modify: `backend/app/gateway/private_work_schemas.py`
- Modify: `backend/app/gateway/services.py`
- Create: `backend/tests/test_automation_dispatcher.py`
- Modify: `backend/tests/test_private_work_run_router.py`
- Modify: `backend/tests/test_gateway_services.py`

**Interfaces:**

- Produces `AutomationDispatcher.dispatch(occurrence_id, *, app) -> AutomationDispatchResult`。
- Produces `start_scheduled_private_run(app, context, thread_id, run_id, prompt, metadata) -> RunRecord`。
- Extends `start_private_run(..., *, run_id: str | None = None, server_context: Mapping[str, object] | None = None)`；两个 keyword只供 app-internal callers，公共 Pydantic model不暴露。
- Produces `ensure_automation_thread(context, task, occurrence) -> str`，fresh mode幂等创建，reuse mode重校验。

- [ ] **Step 1: 写 client non-interactive stripping、deterministic IDs、scope revalidation 和 private-run reuse 失败测试**

```python
def test_public_private_run_strips_non_interactive():
    request = PrivateRunCreateRequest(
        input={"messages": [{"role": "user", "content": "hello"}]},
        context={"non_interactive": True, "project_id": str(uuid.uuid4())},
    )
    assert request.context == {}


async def test_dispatcher_launches_through_private_run(seed, monkeypatch):
    launch = AsyncMock(return_value=seed.run_record)
    dispatcher = AutomationDispatcher(
        seed.factory,
        thread_service=seed.thread_service,
        launch_private_run=launch,
    )
    result = await dispatcher.dispatch(seed.occurrence_id, app=seed.app)
    assert result.run_id == seed.expected_run_id
    launch.assert_awaited_once()
    kwargs = launch.await_args.kwargs
    assert kwargs["context"] is seed.issued_private_context
    assert kwargs["run_id"] == seed.expected_run_id
    assert kwargs["metadata"] == {
        "scheduled_task_id": seed.task_id,
        "scheduled_task_run_id": seed.occurrence_id,
        "scheduled_trigger": "scheduled",
    }
```

Cross-scope and stale capability：

```python
async def test_dispatch_rejects_viewer_downgrade_before_thread_create(seed):
    await seed.downgrade_owner_to_viewer()
    with pytest.raises(AutomationForbidden):
        await seed.dispatcher.dispatch(seed.occurrence_id, app=seed.app)
    seed.thread_service.create.assert_not_awaited()
    seed.launch_private_run.assert_not_awaited()
```

- [ ] **Step 2: 运行 tests，确认 scheduled path仍调用 legacy `start_run`**

Run:

```bash
cd backend
uv run pytest tests/test_automation_dispatcher.py tests/test_private_work_run_router.py tests/test_gateway_services.py -q
```

Expected: FAIL，提示 dispatcher不存在、client `non_interactive`仍保留或 `launch_scheduled_thread_run`仍调用 shared `start_run()`。

- [ ] **Step 3: 把 `non_interactive` 加入 client authority stripping，并允许 internal server context**

在 `_CLIENT_AUTHORITY_FIELDS` 增加：

```python
"non_interactive",
```

`start_private_run` 构造 config后才合并 server-only fields：

```python
trusted_server_context = (
    strip_private_client_fields(server_context)
    if isinstance(server_context, Mapping)
    else {}
)
if server_context and server_context.get("non_interactive") is True:
    trusted_server_context["non_interactive"] = True
config["context"] = {
    **dict(config.get("context", {})),
    **trusted_server_context,
}
create_request = PrivateRunCreate(
    run_id=run_id or str(uuid.uuid4()),
    assistant_id=None,
    metadata=dict(config.get("metadata", {})),
    kwargs={"input": body.input, "config": redact_config_secrets(persisted_config)},
    multitask_strategy="reject",
)
```

Public router永远不传 `server_context` 或 `run_id`。

- [ ] **Step 4: 实现 scheduler-only private run adapter**

```python
async def start_scheduled_private_run(
    *,
    app: Any,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    prompt: str,
    metadata: Mapping[str, object],
) -> RunRecord:
    request = SimpleNamespace(app=app, state=SimpleNamespace(), headers={}, cookies={})
    body = SimpleNamespace(
        input={"messages": [{"role": "user", "content": prompt}]},
        command=None,
        metadata=dict(metadata),
        config=None,
        context={},
        checkpoint_id=None,
        checkpoint=None,
        interrupt_before=None,
        interrupt_after=None,
        stream_mode=None,
        stream_subgraphs=False,
        on_disconnect="continue",
        multitask_strategy="reject",
    )
    return await start_private_run(
        body,
        thread_id,
        request,
        context,
        run_id=run_id,
        server_context={"non_interactive": True},
    )
```

删除或停止使用 `launch_scheduled_thread_run()` 的 legacy shared-run实现；兼容 import若暂时保留，必须在 M4 cutover后抛 `AutomationCutover`，不能 fallback。

- [ ] **Step 5: 实现 server context re-resolution 和 Thread preparation**

Dispatcher transaction先按 occurrence→task composite scope加载 rows，然后：

```python
project = await resolve_project_context_in_transaction(
    session,
    uuid.UUID(occurrence.owner_user_id),
    occurrence.project_id,
    request_id,
    lock=True,
)
project.require(Capability.AUTOMATION_MANAGE_OWN)
project.require(Capability.PRIVATE_WORK_CREATE)
project.require(Capability.SHARED_ASSETS_EXECUTE)
context = PrivateWorkContext.from_project(project)
```

确认 task未 frozen/deleted、status enabled或manual允许的paused状态、`task.version == occurrence.task_version`。写 resolved membership fields后commit，再调用 Thread service。

Fresh mode调用：

```python
ThreadAgentRef(asset_id=task.agent_asset_id, scope=task.agent_scope)
```

若 deterministic Thread已存在，只接受相同 scope、Agent ref和 `metadata["scheduled_task_run_id"]`；否则 `AutomationConflict`。Reuse mode只读取 task.thread_id并再次验证 active。

- [ ] **Step 6: 启动 private run并把 occurrence转为 running**

Private run成功返回后，scoped transaction执行：

```python
await run_repo.mark_running(
    context.resource_scope,
    occurrence.id,
    thread_id=record.thread_id,
    run_id=record.run_id,
    started_at=record.created_at,
)
```

若 private admission前失败，按分类写 `rejected` 或带 `next_attempt_at` 的 queued；若 M4 run row已经存在，绝不requeue。

- [ ] **Step 7: 运行 dispatcher/private-run tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_automation_dispatcher.py tests/test_private_work_run_router.py tests/test_gateway_services.py -q
```

Expected: PASS。

```bash
git add backend/app/automations/dispatcher.py backend/app/private_work/context.py backend/app/private_work/run_admission.py backend/app/gateway/private_work_schemas.py backend/app/gateway/services.py backend/tests/test_automation_dispatcher.py backend/tests/test_private_work_run_router.py backend/tests/test_gateway_services.py
git commit -m "feat: launch automations through private runtime"
```

---

### Task 7: 实现 completion CAS、restart reconciliation 和 Scheduler poll lifecycle

**Files:**

- Create: `backend/app/automations/reconciliation.py`
- Modify: `backend/app/scheduler/service.py`
- Modify: `backend/app/gateway/deps.py`
- Modify: `backend/app/gateway/app.py`
- Create: `backend/tests/test_automation_reconciliation.py`
- Modify: `backend/tests/test_scheduled_task_service.py`
- Modify: `backend/tests/test_scheduled_task_lifecycle.py`

**Interfaces:**

- Produces `AutomationReconciler.handle_run_completion(record: RunRecord) -> None`。
- Produces `AutomationReconciler.reconcile_restart(now) -> ReconciliationReport`。
- `ScheduledTaskService` constructor改为 `occurrences`、`dispatcher`、`reconciler`；不再接 legacy repositories/launch function。
- `ScheduledTaskService.run_once(now)` 先 reserve due，再反复 claim/dispatch到budget耗尽。

- [ ] **Step 1: 写 metadata非authority、terminal CAS、once/cron parent和restart失败测试**

```python
async def test_completion_uses_persisted_run_scope_not_forged_metadata(seed):
    record = seed.run_record(
        run_id=seed.owner_run_id,
        metadata={
            "scheduled_task_id": seed.outsider_task_id,
            "scheduled_task_run_id": seed.outsider_occurrence_id,
        },
        status=RunStatus.success,
    )
    await seed.reconciler.handle_run_completion(record)
    assert (await seed.owner_occurrence()).status == "success"
    assert (await seed.outsider_occurrence()).status == "running"


async def test_duplicate_completion_is_idempotent(seed):
    record = seed.run_record(status=RunStatus.success)
    await seed.reconciler.handle_run_completion(record)
    await seed.reconciler.handle_run_completion(record)
    occurrence = await seed.occurrence()
    assert occurrence.status == "success"
    assert (await seed.task()).run_count == 1
```

Restart cases：

```python
async def test_restart_requeues_only_when_private_run_row_is_absent(seed):
    await seed.expire_launching_occurrence(with_private_run=False)
    report = await seed.reconciler.reconcile_restart(seed.now)
    assert report.requeued == 1
    assert (await seed.occurrence()).status == "queued"


async def test_restart_interrupts_admitted_run_without_replay(seed):
    await seed.expire_launching_occurrence(with_private_run=True, run_status="running")
    report = await seed.reconciler.reconcile_restart(seed.now)
    assert report.interrupted == 1
    assert (await seed.occurrence()).status == "interrupted"
    assert (await seed.private_run()).status == "interrupted"
```

- [ ] **Step 2: 运行 tests，确认 legacy startup全表 sweep语义失败**

Run:

```bash
cd backend
uv run pytest tests/test_automation_reconciliation.py tests/test_scheduled_task_service.py tests/test_scheduled_task_lifecycle.py -q
```

Expected: FAIL，原因包含 reconciler不存在、completion信任 metadata或 startup仍调用 `mark_stale_active_runs()`。

- [ ] **Step 3: 实现 scoped completion lookup 和 parent update**

Lookup顺序固定：

```python
run_row = await PrivateRunRepository(session).get_unscoped_for_completion(record.run_id)
if run_row is None:
    return
scope = PrivateResourceScope(
    project_id=str(run_row.project_id),
    owner_user_id=run_row.owner_user_id,
)
occurrence = await ScheduledTaskRunRepository(session).get_by_agent_run_id(
    scope,
    record.run_id,
    lock=True,
)
```

`get_unscoped_for_completion` 只存在于明确命名的 completion adapter，返回 scope coordinates，不暴露为普通业务 repository方法。随后校验 task/occurrence/run composite关系并 terminal CAS。

Outcome mapping：

```python
RUN_TO_OCCURRENCE = {
    RunStatus.success: ("success", None),
    RunStatus.error: ("failed", "AUTOMATION_RUN_FAILED"),
    RunStatus.timeout: ("failed", "AUTOMATION_RUN_TIMEOUT"),
    RunStatus.interrupted: ("interrupted", "AUTOMATION_RUN_INTERRUPTED"),
}
```

Cron保持 enabled；once success→completed、failed→failed、interrupted→cancelled。只有第一次terminal CAS成功时increment parent run_count。

- [ ] **Step 4: 实现 restart reconciliation**

在受支持的单 Gateway startup中：

```python
if occurrence.status == "launching" and occurrence.lease_expires_at < now:
    if private_run is None:
        await occurrences.requeue(scope, occurrence.id, now=now)
    elif private_run.status in {"pending", "running"}:
        await private_runs.update_status(
            scope=scope,
            run_id=private_run.run_id,
            status="interrupted",
            error="Gateway restarted before automation run completion",
        )
        await occurrences.finish(
            scope,
            occurrence.id,
            status="interrupted",
            error_code="AUTOMATION_GATEWAY_RESTARTED",
            error_message="The automation run was interrupted by a Gateway restart.",
            finished_at=now,
        )
```

Running occurrence缺 run row→failed `AUTOMATION_RUN_MISSING`；terminal rows不变。日志只写 counts/error code，不写 task/title/prompt/IDs。

- [ ] **Step 5: 重写 Scheduler service 为 orchestration-only poller**

```python
async def run_once(self, *, now: datetime) -> None:
    await self._occurrences.reserve_due(now=now, limit=self._max_concurrent_runs)
    while True:
        occurrence = await self._occurrences.claim_next(
            now=now,
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
        )
        if occurrence is None:
            return
        await self._dispatcher.dispatch(occurrence.id, app=self._app)
```

`start()` 先 `reconcile_restart()` 再开 poll loop。`scheduler.enabled=false` 时 app仍初始化 service/dispatcher供manual trigger，但不调用 `start()`。

- [ ] **Step 6: 将 completion hook 和 app lifecycle 接到新 reconciler**

`get_run_context()` 使用：

```python
on_run_completed=(
    request.app.state.automation_reconciler.handle_run_completion
    if request.app.state.automation_reconciler is not None
    else None
)
```

Gateway startup实例化 services顺序：repositories/session factory→cutover/readiness→occurrences→reconciler→dispatcher→scheduler。Shutdown先停止 scheduler，再停止 channel/runtime。

- [ ] **Step 7: 运行 lifecycle tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_automation_reconciliation.py tests/test_scheduled_task_service.py tests/test_scheduled_task_lifecycle.py -q
```

Expected: PASS。

```bash
git add backend/app/automations/reconciliation.py backend/app/scheduler/service.py backend/app/gateway/deps.py backend/app/gateway/app.py backend/tests/test_automation_reconciliation.py backend/tests/test_scheduled_task_service.py backend/tests/test_scheduled_task_lifecycle.py
git commit -m "feat: reconcile automation executions"
```

---

### Task 8: 把 Automation 接入 membership/project freeze 与 restore

**Files:**

- Modify: `backend/app/private_work/retention.py`
- Modify: `backend/app/projects/membership_service.py`
- Modify: `backend/app/projects/lifecycle_service.py`
- Modify: `backend/tests/test_private_run_authorization.py`
- Modify: `backend/tests/test_project_membership_service.py`
- Modify: `backend/tests/test_project_lifecycle_service.py`
- Create: `backend/tests/test_automation_retention.py`

**Interfaces:**

- Extends `RetentionChange` with `automation_ids: tuple[str, ...]` and `occurrence_ids: tuple[str, ...]`。
- `PrivateWorkRetentionService.freeze_owner()` pauses/freeze definitions and cancels queued occurrence in the caller transaction。
- `restore_owner(s)` clears `frozen_at` but leaves task status paused and `next_run_at=None`。
- Existing membership/lifecycle service lock order and post-commit local run notification remain unchanged。

- [ ] **Step 1: 写 freeze/restore、Viewer downgrade和project suspend失败测试**

```python
async def test_freeze_pauses_tasks_and_cancels_queued_occurrences(seed):
    task = await seed.create_task(status="enabled", next_run_at=seed.now)
    occurrence = await seed.create_occurrence(task, status="queued")
    async with seed.factory() as session, session.begin():
        change = await PrivateWorkRetentionService.freeze_owner(
            session,
            project_id=seed.project_id,
            owner_user_id=seed.owner_id,
            now=seed.now,
        )
    assert change.automation_ids == (task.id,)
    assert change.occurrence_ids == (occurrence.id,)
    frozen = await seed.task(task.id)
    assert frozen.status == "paused"
    assert frozen.frozen_at == seed.now
    assert frozen.next_run_at is None


async def test_restore_does_not_auto_resume_task(seed):
    await seed.freeze_owner()
    await seed.restore_owner()
    restored = await seed.task()
    assert restored.frozen_at is None
    assert restored.status == "paused"
    assert restored.next_run_at is None
```

Viewer downgrade service test断言 retention hook在角色从 Runner→Viewer时被调用；当前代码只mark run cancellation，测试应先失败。

- [ ] **Step 2: 运行 retention tests，确认 Automation尚未包含在 change set**

Run:

```bash
cd backend
uv run pytest tests/test_automation_retention.py tests/test_private_run_authorization.py tests/test_project_membership_service.py tests/test_project_lifecycle_service.py -q
```

Expected: FAIL，原因包含 `RetentionChange` 缺字段或 task仍 enabled。

- [ ] **Step 3: 扩展 freeze/restore SQL**

Freeze：

```python
automation_ids = tuple(
    (
        await session.execute(
            update(ScheduledTaskRow)
            .where(
                ScheduledTaskRow.project_id == project_id,
                ScheduledTaskRow.owner_user_id == owner_user_id,
                ScheduledTaskRow.deleted_at.is_(None),
                ScheduledTaskRow.frozen_at.is_(None),
            )
            .values(
                status="paused",
                next_run_at=None,
                frozen_at=frozen_at,
                version=ScheduledTaskRow.version + 1,
                updated_at=frozen_at,
            )
            .returning(ScheduledTaskRow.id)
        )
    ).scalars().all()
)
```

同事务把这些 task的 queued occurrences设 cancelled；launching/running由 existing authorization service按 composite run关系写 M4 cancellation marker。

Restore只执行：

```python
.values(frozen_at=None, status="paused", next_run_at=None, updated_at=restored_at)
```

- [ ] **Step 4: 让 Viewer downgrade 复用 freeze hook**

在 `MembershipService.change_role()` 的 non-Viewer→Viewer分支中，mark revoked后调用：

```python
await self._retention.freeze_owner(
    self.repository.session,
    project_id=project.id,
    owner_user_id=target.user_id,
    now=self._clock(),
)
```

Remove/leave/suspend/pending deletion已调用同一 hook，不新增第二种锁序。Restore/rejoin使用现有 restore hook，保持paused。

- [ ] **Step 5: 运行 lifecycle tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_automation_retention.py tests/test_private_run_authorization.py tests/test_project_membership_service.py tests/test_project_lifecycle_service.py -q
```

Expected: PASS。

```bash
git add backend/app/private_work/retention.py backend/app/projects/membership_service.py backend/app/projects/lifecycle_service.py backend/tests/test_automation_retention.py backend/tests/test_private_run_authorization.py backend/tests/test_project_membership_service.py backend/tests/test_project_lifecycle_service.py
git commit -m "feat: freeze automations on authorization loss"
```

---

### Task 9: 暴露 strict project Automation API 并关闭 legacy authority

**Files:**

- Create: `backend/app/gateway/automation_schemas.py`
- Create: `backend/app/gateway/routers/project_automations.py`
- Modify: `backend/app/gateway/routers/__init__.py`
- Modify: `backend/app/gateway/routers/scheduled_tasks.py`
- Modify: `backend/app/gateway/deps.py`
- Modify: `backend/app/gateway/app.py`
- Create: `backend/tests/test_project_automations_router.py`
- Modify: `backend/tests/test_scheduled_task_router.py`
- Modify: `backend/tests/test_scheduled_task_router_behavior.py`
- Modify: `backend/tests/test_private_work_route_dependencies.py`

**Interfaces:**

- Base path `/api/projects/{project_id}/automations`。
- Readiness endpoint不被 project-open dependency阻断；其余 endpoints必须 `AutomationRoute + require_project_automation_open`。
- Manual trigger要求 `Idempotency-Key` UUID header。
- Legacy router在 expand阶段冻结 mutation，在 cutover complete后所有 route返回 `409 AUTOMATION_CUTOVER`。

- [ ] **Step 1: 写 route contract、capability、strict body、404/403/409/429/503失败测试**

```python
def test_project_automation_routes_are_mounted(app):
    paths = {route.path for route in app.routes}
    assert "/api/projects/{project_id}/automations" in paths
    assert "/api/projects/{project_id}/automations/{task_id}/trigger" in paths
    assert "/api/projects/{project_id}/automations/{task_id}/runs" in paths


async def test_create_uses_server_context_and_strips_authority(seed):
    body = AutomationCreateRequest(
        title="Daily report",
        prompt="Summarize my private work",
        context_mode="fresh_thread_per_run",
        agent_asset_id=seed.agent_id,
        agent_scope="system",
        schedule_type="cron",
        schedule_spec={"cron": "0 9 * * *"},
        timezone="Asia/Shanghai",
    )
    result = await create_automation.__wrapped__(
        body=body,
        context=seed.context,
        service=seed.service,
    )
    assert result.title == "Daily report"
    seed.service.create.assert_awaited_once_with(seed.context, body.to_command())
```

Strict request测试额外传 `owner_user_id`、`project_id`、`non_interactive`，Expected: Pydantic validation error。

Manual header：

```python
async def test_trigger_requires_uuid_idempotency_key(client, project_id, task_id):
    response = client.post(
        f"/api/projects/{project_id}/automations/{task_id}/trigger",
        headers={"Idempotency-Key": "not-a-uuid"},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: 运行 router tests，确认 project routes不存在**

Run:

```bash
cd backend
uv run pytest tests/test_project_automations_router.py tests/test_scheduled_task_router.py tests/test_scheduled_task_router_behavior.py tests/test_private_work_route_dependencies.py -q
```

Expected: FAIL，提示 routes未挂载或 legacy router仍直接调用 user-scoped repository。

- [ ] **Step 3: 定义 strict request/response models 与 route dependency**

```python
class StrictAutomationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AutomationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                raise automation_http_exception(
                    AutomationInvalid(request_id),
                ) from None

        return handler


router = APIRouter(
    prefix="/api/projects/{project_id}/automations",
    tags=["project-automations"],
    route_class=AutomationRoute,
    dependencies=[Depends(require_project_automation_open)],
)

readiness_router = APIRouter(
    prefix="/api/projects/{project_id}/automations",
    tags=["project-automations"],
    route_class=AutomationRoute,
)


class AutomationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1)
    context_mode: Literal["fresh_thread_per_run", "reuse_thread"]
    thread_id: uuid.UUID | None = None
    agent_asset_id: uuid.UUID
    agent_scope: Literal["project", "system"]
    schedule_type: Literal["once", "cron"]
    schedule_spec: dict[str, object]
    timezone: str = Field(min_length=1, max_length=64)
```

Response不返回 owner/membership/lease/hash；datetime统一ISO offset。
`GET /readiness` 只注册在 `readiness_router`；其余endpoints只注册在带
`require_project_automation_open` 的 `router`，并在 `app.py` 中分别mount，避免migration状态被总router
dependency提前截断。

- [ ] **Step 4: 实现 read/mutation endpoints 与统一错误映射**

Endpoints exact：

```text
GET    /readiness
GET    /
POST   /
GET    /{task_id}
PATCH  /{task_id}
DELETE /{task_id}
POST   /{task_id}/pause
POST   /{task_id}/resume
POST   /{task_id}/trigger
GET    /{task_id}/runs
GET    /threads/{thread_id}
```

Read用 `private_work.read_own`；mutation调用 service/occurrence层，Router不直接访问 session/repository。`limit` 1..100、`offset >= 0`。Error catch只接 `AutomationError` 并交给 mapper。

- [ ] **Step 5: 接入 app state 和 Scheduler/manual dispatch**

`langgraph_runtime` 创建：

```python
app.state.automation_cutover_guard = AutomationCutoverGuard(sf, revisions)
app.state.automation_service = ProjectAutomationService(sf)
app.state.automation_occurrences = AutomationOccurrenceService(
    sf,
    max_concurrent_runs=config.scheduler.max_concurrent_runs,
)
app.state.automation_reconciler = AutomationReconciler(sf)
app.state.automation_dispatcher = AutomationDispatcher(sf, app=app)
```

Manual trigger reserve后立即调用 dispatcher；`scheduler.enabled`不阻断manual。

- [ ] **Step 6: 冻结/关闭 legacy router**

给 legacy routes添加 guard：

```python
async def require_legacy_automation_open(request: Request) -> None:
    await get_automation_cutover_guard(request).require_legacy_open()
```

当 DB已expand但未cutover：GET/list/history只读可用，所有 mutation返回 `409 AUTOMATION_MIGRATION_REQUIRED`。Cutover complete后所有 legacy routes返回 `409 AUTOMATION_CUTOVER`。Legacy router不再触发 run。

- [ ] **Step 7: 运行 router tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_project_automations_router.py tests/test_scheduled_task_router.py tests/test_scheduled_task_router_behavior.py tests/test_private_work_route_dependencies.py -q
```

Expected: PASS。

```bash
git add backend/app/gateway/automation_schemas.py backend/app/gateway/routers/project_automations.py backend/app/gateway/routers/__init__.py backend/app/gateway/routers/scheduled_tasks.py backend/app/gateway/deps.py backend/app/gateway/app.py backend/tests/test_project_automations_router.py backend/tests/test_scheduled_task_router.py backend/tests/test_scheduled_task_router_behavior.py backend/tests/test_private_work_route_dependencies.py
git commit -m "feat: expose project automation API"
```

---

### Task 10: 实现 explicit legacy Automation migration 和运维命令

**Files:**

- Create: `backend/scripts/migrate_automations.py`
- Modify: `backend/scripts/setup_postgres.py`
- Modify: `backend/scripts/check_postgres.py`
- Modify: `scripts/doctor.py`
- Modify: `backend/Makefile`
- Modify: `Makefile`
- Create: `backend/tests/test_automation_migration.py`
- Create: `backend/tests/test_automation_migration_cli.py`
- Modify: `backend/tests/test_setup_postgres.py`
- Modify: `backend/tests/test_check_postgres.py`

**Interfaces:**

- CLI: `make migrate-automations ARGS="--dry-run|--execute --owner-map <json> --backup-dir <path>"`。
- Map value固定 `{project_id, fresh_thread_agent:{asset_id,scope}}`。
- Produces `AutomationMigrationReport(mode, counts, source_key_hash, cutover_complete, empty_install, noop)`；不含私有 values/IDs。
- Execute只接受 revision `0011` 或 `0012`；cutover complete时no-op。

- [ ] **Step 1: 写 parser、redacted inventory、map/relation、idempotency和fail-before-DDL失败测试**

```python
def test_parser_requires_mode_owner_map_and_backup_dir(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "--dry-run",
            "--owner-map",
            str(tmp_path / "owners.json"),
            "--backup-dir",
            str(tmp_path / "backup"),
        ]
    )
    assert args.dry_run is True
    assert args.execute is False


def test_report_does_not_include_private_values(inventory):
    report = render_report(build_migration_plan(inventory, inventory.owner_map))
    encoded = json.dumps(report)
    assert inventory.prompt not in encoded
    assert inventory.title not in encoded
    assert inventory.owner_user_id not in encoded
    assert inventory.thread_id not in encoded
```

Migration behavior：

```python
async def test_fresh_task_requires_explicit_agent_map(m5_legacy_database):
    owner_map = {
        m5_legacy_database.owner_id: {
            "project_id": str(m5_legacy_database.project_id),
        }
    }
    with pytest.raises(AutomationMigrationError, match="fresh thread agent mapping is required"):
        await run_automation_migration(
            m5_legacy_database.url,
            owner_map=owner_map,
            backup_dir=m5_legacy_database.backup_dir,
            execute=False,
        )
```

同时覆盖 reuse Thread map mismatch、inactive Viewer target、Agent not executable、orphan run、unsupported status、fingerprint change、ledger digest conflict、cutover no-op。

- [ ] **Step 2: 运行 migration tests，确认 CLI不存在**

Run:

```bash
cd backend
uv run pytest tests/test_automation_migration.py tests/test_automation_migration_cli.py tests/test_setup_postgres.py tests/test_check_postgres.py -q
```

Expected: collection FAIL，提示 `scripts.migrate_automations` 不存在或 check-db不识别 M5 tables/head。

- [ ] **Step 3: 实现 parser、canonical map 与 redacted inventory**

```python
@dataclass(frozen=True, slots=True)
class AutomationOwnerTarget:
    owner_user_id: str
    project_id: uuid.UUID
    agent_asset_id: uuid.UUID
    agent_scope: str


class FreshThreadAgentMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: uuid.UUID
    scope: Literal["project", "system"]


class AutomationOwnerMapItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: uuid.UUID
    fresh_thread_agent: FreshThreadAgentMap


def normalize_owner_map(raw: Mapping[str, object]) -> tuple[AutomationOwnerTarget, ...]:
    targets: list[AutomationOwnerTarget] = []
    for owner, value in sorted(raw.items()):
        owner_id = str(uuid.UUID(owner))
        item = owner_map_item_schema(value)
        targets.append(
            AutomationOwnerTarget(
                owner_user_id=owner_id,
                project_id=item.project_id,
                agent_asset_id=item.fresh_thread_agent.asset_id,
                agent_scope=item.fresh_thread_agent.scope,
            )
        )
    return tuple(targets)


def owner_map_item_schema(value: object) -> AutomationOwnerMapItem:
    return AutomationOwnerMapItem.model_validate(value)
```

Inventory canonical digest包含source row的所有字段，但report只返回 counts/status aggregates和digest前12字符。

- [ ] **Step 4: 实现 dry-run validation**

Dry-run读取 `0011/0012/0013`，验证：M4 marker complete、每个owner有map、active non-Viewer membership、fresh Agent executable、reuse Thread scope/Agent、task-run parent、存在的Thread/run pointer可通过M4 composite relation、所有status可转换。

Legacy fresh task的thread_id统一计划为NULL；pre-admission skipped row的不存在Thread pointer计划为NULL，并在target digest中记录转换。

- [ ] **Step 5: 实现 staged execute 和 ledger**

Exact sequence：

```python
if revision == "0011_private_artifact_tombstone":
    await asyncio.to_thread(
        command.upgrade,
        alembic_config(engine),
        "0012_project_automation_expand",
    )
await assert_source_fingerprint(engine, inventory.source_fingerprint)
migration_run_id = await execute_staging(engine, plan, inventory)
await asyncio.to_thread(command.upgrade, alembic_config(engine), "head")
await mark_cutover_complete(engine, migration_run_id)
```

Staging写两 domain ledger、scope/Agent/version/frozen fields和occurrence conversion。`migration_ready`前验证source/target row count和digest。`0013`负责最后rename/constraint。

- [ ] **Step 6: 接入 Makefile、setup/check/doctor**

Backend target：

```make
migrate-automations:
	PYTHONPATH=. uv run python scripts/migrate_automations.py $(ARGS)
```

Root target代理 `$(MAKE) -C backend migrate-automations ARGS="$(ARGS)"`，help明确先dry-run。`check_postgres.REQUIRED_TABLES`加入三control tables；doctor只报告 `ready/migration_required/unavailable`，不读prompt或map。

- [ ] **Step 7: 运行 migration/ops tests 并提交**

Run:

```bash
cd backend
uv run pytest tests/test_automation_migration.py tests/test_automation_migration_cli.py tests/test_setup_postgres.py tests/test_check_postgres.py -q
```

Expected: PASS。

```bash
git add backend/scripts/migrate_automations.py backend/scripts/setup_postgres.py backend/scripts/check_postgres.py scripts/doctor.py backend/Makefile Makefile backend/tests/test_automation_migration.py backend/tests/test_automation_migration_cli.py backend/tests/test_setup_postgres.py backend/tests/test_check_postgres.py
git commit -m "feat: migrate legacy automations"
```

---

### Task 11: 建立 account/project-scoped Frontend Automation client

**Files:**

- Create: `frontend/src/core/project-automations/types.ts`
- Create: `frontend/src/core/project-automations/query-keys.ts`
- Create: `frontend/src/core/project-automations/api.ts`
- Create: `frontend/src/core/project-automations/hooks.ts`
- Create: `frontend/src/core/project-automations/readiness.ts`
- Create: `frontend/tests/unit/core/project-automations/types.test.ts`
- Create: `frontend/tests/unit/core/project-automations/query-keys.test.ts`
- Create: `frontend/tests/unit/core/project-automations/api.test.ts`
- Create: `frontend/tests/unit/core/project-automations/hooks.test.tsx`
- Create: `frontend/tests/unit/core/project-automations/readiness.test.tsx`

**Interfaces:**

- `automationRoot(scope) -> ['account', accountId, 'project', projectId, 'automations']`。
- Strict Zod `automationSchema`、`automationRunSchema`、`automationReadinessSchema`。
- API functions全部接 `ProjectClientScope` 和 optional AbortSignal；base URL固定 `/api/projects/{projectId}/automations`。
- Mutations接受scope并使用scoped mutation key；manual trigger生成/接收UUID idempotency key header。

- [ ] **Step 1: 写 strict schema、query key、API URL/header和cache isolation失败测试**

```typescript
it("keys every automation query by account and project", () => {
  expect(
    automationQueryKey(
      { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
      "task",
      "task-1",
    ),
  ).toEqual([
    "account",
    ACCOUNT_ID,
    "project",
    PROJECT_ID,
    "automations",
    "task",
    "task-1",
  ]);
});


it("sends the manual idempotency key only as a header", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(RUN), { status: 200 }),
  );
  await triggerAutomation(SCOPE, "task-1", IDEMPOTENCY_KEY);
  const [, init] = fetchMock.mock.calls[0]!;
  expect(new Headers(init?.headers).get("Idempotency-Key")).toBe(
    IDEMPOTENCY_KEY,
  );
  expect(init?.body).toBeUndefined();
});
```

Strict schema test向 response添加 `owner_user_id` 或 `lease_owner`，Expected: parse throws。

- [ ] **Step 2: 运行 unit tests，确认 modules不存在**

Run:

```bash
cd frontend
pnpm test -- project-automations
```

Expected: FAIL，提示 imports无法解析。

- [ ] **Step 3: 定义 strict Zod contracts**

```typescript
export const automationStatusSchema = z.enum([
  "enabled",
  "paused",
  "completed",
  "failed",
  "cancelled",
]);

export const automationSchema = z
  .object({
    id: z.string().min(1),
    thread_id: z.string().uuid().nullable(),
    context_mode: z.enum(["fresh_thread_per_run", "reuse_thread"]),
    agent_asset_id: z.string().uuid(),
    agent_scope: z.enum(["project", "system"]),
    title: z.string().min(1),
    prompt: z.string(),
    schedule_type: z.enum(["once", "cron"]),
    schedule_spec: z.record(z.string(), z.unknown()),
    timezone: z.string().min(1),
    status: automationStatusSchema,
    next_run_at: z.string().datetime({ offset: true }).nullable(),
    last_run_at: z.string().datetime({ offset: true }).nullable(),
    last_outcome: z.string().nullable(),
    last_error_code: z.string().nullable(),
    run_count: z.number().int().nonnegative(),
    version: z.number().int().positive(),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();
```

Run schema包含public status/error message但不含lease/hash/resolved membership。

- [ ] **Step 4: 实现 scoped API 和 readiness**

```typescript
function automationBaseURL(scope: ProjectClientScope): string {
  const parsed = projectClientScopeSchema.parse(scope);
  return `${getBackendBaseURL()}/api/projects/${encodeURIComponent(
    parsed.projectId,
  )}/automations`;
}

export async function triggerAutomation(
  scope: ProjectClientScope,
  taskId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<AutomationRun> {
  const response = await fetchWithAuth(
    `${automationBaseURL(scope)}/${encodeURIComponent(taskId)}/trigger`,
    {
      method: "POST",
      headers: { "Idempotency-Key": z.string().uuid().parse(idempotencyKey) },
      signal,
    },
  );
  return readAutomationResponse(response, automationRunSchema);
}
```

所有 mutation body只含contract fields/expected_version；错误通过 existing Gateway error parser转安全 public message。

- [ ] **Step 5: 实现 query/mutation hooks 与 scope cleanup兼容**

Hooks从 `usePrivateWorkAccess().scope`读取 account/project；scope null时disabled。Example：

```typescript
export function useProjectAutomations(enabled = true) {
  const access = usePrivateWorkAccess();
  return useQuery({
    queryKey: access.scope
      ? automationQueryKey(access.scope, "list")
      : ["automations", "inactive"],
    queryFn: ({ signal }) => fetchAutomations(access.scope!, signal),
    enabled: enabled && access.scope !== null,
    retry: false,
  });
}
```

Mutation success只invalidate同scope root。Provider unmount现有 `transitionPrivateWorkScope` 必须增加 automation root cancellation/removal；先cancel queries/mutations，再remove。

- [ ] **Step 6: 运行 Frontend core tests 并提交**

Run:

```bash
cd frontend
pnpm test -- project-automations
```

Expected: PASS。

```bash
git add frontend/src/core/project-automations frontend/src/core/private-work frontend/tests/unit/core/project-automations
git commit -m "feat: add scoped automation client"
```

---

### Task 12: 构建 Project Automation workbench 页面

**Files:**

- Create: `frontend/src/components/projects/automations/automation-form.tsx`
- Create: `frontend/src/components/projects/automations/automation-workbench.tsx`
- Create: `frontend/src/components/projects/automations/project-automations-page.tsx`
- Create: `frontend/src/app/projects/[project_slug]/automations/page.tsx`
- Modify: `frontend/src/components/workspace/scheduled-task-schedule-input.tsx`
- Modify: `frontend/src/core/scheduled-tasks/recipes.ts`
- Create: `frontend/tests/unit/components/projects/automations/automation-form.test.tsx`
- Create: `frontend/tests/unit/components/projects/automations/automation-workbench.test.tsx`
- Create: `frontend/tests/unit/components/projects/automations/project-automations-page.test.tsx`

**Interfaces:**

- `ProjectAutomationsPage({project})` 从 current project + scoped hooks渲染 readiness/list/workbench。
- `AutomationForm` 接 `mode`, `initial`, `agents`, `canSubmit`, `onSubmit`；不拥有network state。
- `AutomationWorkbench` 接 typed records和callbacks；Viewer无mutation callbacks。
- 复用 `ScheduledTaskScheduleInput` 的纯 schedule value；不复用 legacy hooks/URLs。

- [ ] **Step 1: 写 Viewer、scheduler-disabled、create/edit/history/conflict state 失败测试**

```typescript
it("renders Viewer history without mutation controls", () => {
  render(
    <AutomationWorkbench
      automations={[AUTOMATION]}
      selected={AUTOMATION}
      runs={[RUN]}
      permissions={{ canRead: true, canManage: false, canExecute: false }}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getByText(AUTOMATION.title)).toBeVisible();
  expect(screen.getByTestId("automation-run-list")).toBeVisible();
  expect(screen.queryByRole("button", { name: "立即运行" })).toBeNull();
  expect(screen.queryByRole("button", { name: "删除" })).toBeNull();
});


it("shows scheduler disabled while keeping manual trigger available", () => {
  renderProjectPage({
    readiness: { ...READY, scheduler_enabled: false },
    capabilities: [
      "private_work.read_own",
      "private_work.create",
      "shared_assets.execute",
      "automation.manage_own",
    ],
  });
  expect(screen.getByTestId("automation-scheduler-disabled")).toBeVisible();
  expect(screen.getByRole("button", { name: "立即运行" })).toBeEnabled();
});
```

Form test验证prompt不会出现在URL/localStorage，mutation完成后form state清空。

- [ ] **Step 2: 运行 component tests，确认 project page不存在**

Run:

```bash
cd frontend
pnpm test -- components/projects/automations
```

Expected: FAIL，提示 modules无法解析。

- [ ] **Step 3: 抽出可复用 schedule/recipe presentation contract**

保持 legacy `ScheduledTaskScheduleInput` URL/API无感，只导出通用别名：

```typescript
export type AutomationScheduleValue = ScheduleValue;
export { ScheduledTaskScheduleInput as AutomationScheduleInput };
```

Recipes只作为前端初始 title/prompt/schedule value，不包含project/owner/Agent authority。Form提交前必须由用户选择一个server catalog Agent。

- [ ] **Step 4: 实现 form 与 permission model**

```typescript
export function automationPermissions(capabilities: ProjectCapability[]) {
  const canRead = capabilities.includes("private_work.read_own");
  const canManage = capabilities.includes("automation.manage_own");
  const canExecute =
    canManage &&
    capabilities.includes("private_work.create") &&
    capabilities.includes("shared_assets.execute");
  return { canRead, canManage, canExecute };
}
```

Form校验：trimmed title/prompt、fresh mode no thread、reuse mode UUID thread、Agent ref required、once future、cron 5 fields、timezone non-empty。Edit锁定schedule type/context mode，与backend contract一致。

- [ ] **Step 5: 实现 workbench 与 page states**

Project page state顺序：

```typescript
if (readiness.isLoading) return <AutomationPageSkeleton />;
if (readiness.data?.status === "migration_required") {
  return <AutomationUnavailableState code={readiness.data.code} />;
}
if (readiness.data?.status === "unavailable" || readiness.error) {
  return <AutomationRetryState onRetry={() => void readiness.refetch()} />;
}
return <AutomationWorkbench />;
```

Workbench提供 list/filter、create/edit、pause/resume/manual/delete、run history、Thread link和409/429/503 refresh message。Manual trigger每次用户动作生成 `crypto.randomUUID()`；mutation retry复用同一key。

- [ ] **Step 6: 创建 route wrapper**

```tsx
"use client";

import { ProjectAutomationsPage } from "@/components/projects/automations/project-automations-page";
import { useCurrentProject } from "@/components/projects/project-context";

export default function ProjectAutomationsRoute() {
  return <ProjectAutomationsPage project={useCurrentProject()} />;
}
```

- [ ] **Step 7: 运行 component tests 并提交**

Run:

```bash
cd frontend
pnpm test -- components/projects/automations
```

Expected: PASS。

```bash
git add frontend/src/components/projects/automations frontend/src/app/projects/[project_slug]/automations/page.tsx frontend/src/components/workspace/scheduled-task-schedule-input.tsx frontend/src/core/scheduled-tasks/recipes.ts frontend/tests/unit/components/projects/automations
git commit -m "feat: add project automation workbench"
```

---

### Task 13: 接入项目导航、Chat入口、i18n和静态模式门禁

**Files:**

- Modify: `frontend/src/core/projects/features.ts`
- Modify: `frontend/src/components/projects/project-nav.tsx`
- Modify: `frontend/src/components/workspace/thread-scheduled-tasks-link.tsx`
- Modify: `frontend/src/components/workspace/chats/scoped-chat-page.tsx`
- Modify: `frontend/src/components/projects/private-work/project-chat-page.tsx`
- Modify: `frontend/src/core/i18n/locales/types.ts`
- Modify: `frontend/src/core/i18n/locales/en-US.ts`
- Modify: `frontend/src/core/i18n/locales/zh-CN.ts`
- Modify: `frontend/tests/unit/components/projects/project-shell.test.tsx`
- Modify: `frontend/tests/unit/components/projects/private-work/project-chat.test.tsx`
- Modify: `frontend/tests/unit/components/projects/private-work/static-private-work-entry.test.tsx`
- Create: `frontend/tests/unit/components/projects/project-automation-entry.test.tsx`

**Interfaces:**

- 新增编译期常量 `PROJECT_AUTOMATION`，本任务保持 `false as const`。
- `projectNavigationItems()` 显式接收 `automationReady` 与 `automationFeatureEnabled`；入口还要求 `private_work.read_own`。
- `ThreadScheduledTasksLink` 由 caller提供 `href`，组件不再内置 legacy URL。
- Project Chat只生成 `/projects/{slug}/automations?thread_id={uuid}`；workspace chat继续生成 legacy URL。

- [ ] **Step 1: 写 nav/capability/readiness/static/chat-link失败测试**

```typescript
it("shows project automations only when every gate is open", () => {
  const project = projectFixture({
    capabilities: ["private_work.read_own", "automation.manage_own"],
  });
  expect(projectNavigationItems(project, true, true, true, true)).toContainEqual(
    expect.objectContaining({
      href: `/projects/${project.slug}/automations`,
      label: "Automations",
    }),
  );
  expect(projectNavigationItems(project, true, true, false, true)).not.toContainEqual(
    expect.objectContaining({ label: "Automations" }),
  );
  expect(projectNavigationItems(project, true, true, true, false)).not.toContainEqual(
    expect.objectContaining({ label: "Automations" }),
  );
});


it("uses the project automation route from project chat", () => {
  renderProjectChat({ projectSlug: "alpha", threadId: THREAD_ID });
  expect(screen.getByRole("link", { name: "Automations" })).toHaveAttribute(
    "href",
    `/projects/alpha/automations?thread_id=${THREAD_ID}`,
  );
  expect(screen.queryByRole("link", { name: "Automations" }))
    .not.toHaveAttribute("href", expect.stringContaining("/workspace/scheduled-tasks"));
});
```

Static test设置 `NEXT_PUBLIC_STATIC_WEBSITE_ONLY=true`，断言没有Automation link且没有 `/automations/readiness` fetch。

- [ ] **Step 2: 运行入口测试，确认 gate/link尚未实现**

Run:

```bash
cd frontend
pnpm test -- project-automation-entry project-shell project-chat static-private-work-entry
```

Expected: FAIL，提示 `PROJECT_AUTOMATION`、Automation nav item或link `href` contract不存在。

- [ ] **Step 3: 增加关闭状态的编译期 flag和纯函数入口判定**

```typescript
export const PROJECT_AUTOMATION = false as const;


export function projectAutomationEntryEnabled(
  featureEnabled: boolean,
  staticWebsiteOnly: boolean,
  canReadPrivateWork: boolean,
  readiness: "ready" | "migration_required" | "unavailable" | undefined,
): boolean {
  return (
    featureEnabled &&
    !staticWebsiteOnly &&
    canReadPrivateWork &&
    readiness === "ready"
  );
}
```

`ProjectNavigationLinks` 仅在feature非static且用户能读private work时调用 `useProjectAutomationReadiness`。Viewer具有 `private_work.read_own` 时可看到入口；mutation controls由Task 12 permission model隐藏。

- [ ] **Step 4: 参数化Thread link并分别接project/workspace URL**

```tsx
export function ThreadScheduledTasksLink({
  href,
  label,
}: {
  href: string;
  label: string;
}) {
  return (
    <Button variant="outline" size="sm" asChild>
      <Link aria-label={label} href={href}>
        <CalendarClock aria-hidden />
        <span className="hidden sm:inline">{label}</span>
      </Link>
    </Button>
  );
}
```

Workspace caller传 `href=/workspace/scheduled-tasks?thread_id=...`；project caller传：

```typescript
const automationHref = `/projects/${encodeURIComponent(
  project.slug,
)}/automations?thread_id=${encodeURIComponent(threadId)}`;
```

Project Automation page只把query中的UUID作为form初始reuse Thread，不把它写入localStorage或mutation cache。

- [ ] **Step 5: 增加中英文文案并运行测试**

Locales新增强类型字段：`project.automations`、`automation.create`、`automation.runNow`、`automation.schedulerDisabled`、`automation.migrationRequired`、`automation.retry`、`automation.history`。中文使用“自动化”，英文使用“Automations”。

Run:

```bash
cd frontend
pnpm test -- project-automation-entry project-shell project-chat static-private-work-entry
```

Expected: PASS。

- [ ] **Step 6: 提交 gated entry**

```bash
git add frontend/src/core/projects/features.ts frontend/src/components/projects/project-nav.tsx frontend/src/components/workspace/thread-scheduled-tasks-link.tsx frontend/src/components/workspace/chats/scoped-chat-page.tsx frontend/src/components/projects/private-work/project-chat-page.tsx frontend/src/core/i18n/locales frontend/tests/unit/components/projects/project-shell.test.tsx frontend/tests/unit/components/projects/private-work/project-chat.test.tsx frontend/tests/unit/components/projects/private-work/static-private-work-entry.test.tsx frontend/tests/unit/components/projects/project-automation-entry.test.tsx
git commit -m "feat: gate project automation entry"
```

---

### Task 14: 完成 Scheduler配置、Gateway wiring和blocking-I/O门禁

**Files:**

- Modify: `backend/packages/harness/deerflow/config/scheduler_config.py`
- Modify: `config.example.yaml`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/deps.py`
- Create: `backend/tests/test_automation_app_wiring.py`
- Create: `backend/tests/test_automation_scheduler_config.py`
- Create: `backend/tests/blocking_io/test_automations.py`
- Modify: `backend/tests/blocking_io/test_gate_smoke.py`
- Modify: `backend/tests/test_private_work_app_wiring.py`

**Interfaces:**

- 继续使用现有五个配置项：`enabled`、`poll_interval_seconds`、`lease_seconds`、`max_concurrent_runs`、`min_once_delay_seconds`。
- Gateway lifespan只创建一个Scheduler task；disabled时不创建poll task，但保留manual dispatcher。
- DB/API async路径不直接调用同步Alembic、文件读写、sleep或blocking HTTP。
- Scheduler structured logs只包含counts、status、request ID和digest prefix。

- [ ] **Step 1: 写配置边界、单例生命周期和disabled行为失败测试**

```python
def test_scheduler_config_rejects_lease_not_greater_than_poll():
    with pytest.raises(ValidationError):
        SchedulerConfig(
            enabled=True,
            poll_interval_seconds=30,
            lease_seconds=30,
            max_concurrent_runs=4,
            min_once_delay_seconds=60,
        )


async def test_disabled_scheduler_keeps_manual_dispatcher(app_factory):
    app = app_factory(scheduler_enabled=False)
    async with app.router.lifespan_context(app):
        assert app.state.automation_dispatcher is not None
        assert app.state.automation_scheduler_task is None
```

补充两次lifespan startup测试，断言每个app实例最多一个poll task且shutdown后task已await/cancel。

- [ ] **Step 2: 运行 wiring/config/blocking tests，确认 lifecycle尚未闭合**

Run:

```bash
cd backend
uv run pytest tests/test_automation_app_wiring.py tests/test_automation_scheduler_config.py tests/test_private_work_app_wiring.py tests/blocking_io/test_automations.py tests/blocking_io/test_gate_smoke.py -q
```

Expected: FAIL，提示Automation app state、task lifecycle或blocking detector fixture不存在。

- [ ] **Step 3: 固化配置校验与示例**

```python
@model_validator(mode="after")
def validate_scheduler_timing(self) -> "SchedulerConfig":
    if self.poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    if self.lease_seconds <= self.poll_interval_seconds:
        raise ValueError("lease_seconds must exceed poll_interval_seconds")
    if self.max_concurrent_runs <= 0:
        raise ValueError("max_concurrent_runs must be positive")
    if self.min_once_delay_seconds < 0:
        raise ValueError("min_once_delay_seconds must be non-negative")
    return self
```

`config.example.yaml` 保留保守默认值并写明：M5只支持single Gateway，lease只覆盖admission，不是Worker execution lease。

- [ ] **Step 4: 实现可测试的Scheduler lifespan**

```python
async def start_automation_scheduler(app: FastAPI) -> None:
    app.state.automation_scheduler_task = None
    if not app.state.config.scheduler.enabled:
        return
    await app.state.automation_cutover_guard.require_scheduler_open()
    app.state.automation_scheduler_task = asyncio.create_task(
        app.state.automation_scheduler.run_forever(),
        name="project-automation-scheduler",
    )


async def stop_automation_scheduler(app: FastAPI) -> None:
    task = app.state.automation_scheduler_task
    app.state.automation_scheduler_task = None
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
```

Marker incomplete/migration required时startup记录safe status并保持poll task为None；Gateway继续启动以提供readiness/migration错误，不回退legacy poll。

- [ ] **Step 5: 把Automation路径加入blocking-I/O扫描**

`test_automations.py` 对 `app.automations`、project router、Scheduler和migration request path运行static/runtime detector。唯一允许的同步Alembic调用必须位于migration CLI并由 `asyncio.to_thread()` 包裹；request path不得导入 `alembic.command`。

- [ ] **Step 6: 运行focused wiring/blocking tests并提交**

Run:

```bash
cd backend
uv run pytest tests/test_automation_app_wiring.py tests/test_automation_scheduler_config.py tests/test_private_work_app_wiring.py tests/blocking_io/test_automations.py tests/blocking_io/test_gate_smoke.py -q
```

Expected: PASS。

```bash
git add backend/packages/harness/deerflow/config/scheduler_config.py config.example.yaml backend/app/gateway/app.py backend/app/gateway/deps.py backend/tests/test_automation_app_wiring.py backend/tests/test_automation_scheduler_config.py backend/tests/blocking_io/test_automations.py backend/tests/blocking_io/test_gate_smoke.py backend/tests/test_private_work_app_wiring.py
git commit -m "feat: wire project automation scheduler"
```

---

### Task 15: 建立真实 PostgreSQL scope、并发和授权门禁

**Files:**

- Create: `backend/tests/support/m5_automation.py`
- Create: `backend/tests/integration/test_m5_project_automation_postgres.py`
- Modify: `.github/workflows/project-foundation-postgres-tests.yml`

**Interfaces:**

- Fixture只从 `POSTGRES_TEST_URL` 连接admin database，并创建随机 `deerflow_test_{uuid}` 数据库。
- Matrix包含 project A owner A、project A owner B、project A Viewer、project B owner和system_admin。
- Integration必须使用真实 scoped repositories、transactions和constraints，不mock session/locks。

- [ ] **Step 1: 写真实数据库fixture和隔离矩阵失败测试**

```python
@pytest.mark.parametrize(
    ("actor_name", "target_name", "expected_status"),
    [
        ("owner_a", "owner_a", 200),
        ("owner_b", "owner_a", 404),
        ("project_b_owner", "owner_a", 404),
        ("system_admin", "owner_a", 404),
    ],
)
async def test_definition_id_probe_is_project_and_owner_scoped(
    m5_app,
    m5_seed,
    actor_name,
    target_name,
    expected_status,
):
    response = await m5_app.get_automation(
        actor=m5_seed.actor(actor_name),
        project_id=m5_seed.project_for(target_name),
        task_id=m5_seed.task_for(target_name),
    )
    assert response.status_code == expected_status
```

同一matrix覆盖list/pagination/update/delete/history/thread reverse lookup；Viewer只能读自己的行，不能trigger；Admin不能读其他owner；system_admin没有private override。

- [ ] **Step 2: 写真实并发、constraint、revocation和run admission失败测试**

```python
async def test_two_scheduler_claims_create_one_occurrence(m5_database, scheduled_task):
    first, second = await asyncio.gather(
        reserve_and_claim(m5_database, scheduled_task, owner="scheduler-a"),
        reserve_and_claim(m5_database, scheduled_task, owner="scheduler-b"),
    )
    claimed = [item for item in (first, second) if item is not None]
    assert len(claimed) == 1
    assert await count_occurrences(m5_database, scheduled_task.id) == 1
```

本文件还必须断言：

- 同manual idempotency key并发返回同一occurrence；
- 多transaction并发claim不超过global cap；
- composite FK拒绝跨project/owner task、Thread或run pointer；
- downgrade/remove/leave/project pending deletion冻结definition并取消queued occurrence；
- fresh/reuse Thread均记录exact Agent version snapshot；
- restart reconciliation对已admitted run只写`interrupted`，不调用第二次`start_private_run()`；
- M4 guard在current revision=`0013_project_automation_finalize`时仍ready。

- [ ] **Step 3: 运行两次定向integration，确认测试先失败**

Run:

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/integration/test_m5_project_automation_postgres.py -q
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/integration/test_m5_project_automation_postgres.py -q
```

Expected: implementation未完成时FAIL；实现完成后两次均PASS且数据库名均以`deerflow_test_`开头。若本地没有变量则测试明确SKIP，但不能执行Task 18完成标记。

- [ ] **Step 4: 将M5 runtime integration加入固定CI**

Workflow job/name改为 `M1, M2, M3, M4 and M5 PostgreSQL gates`，pytest文件列表追加：

```yaml
          tests/integration/test_m5_project_automation_postgres.py
```

保留进入pytest前的 `POSTGRES_TEST_URL` 硬失败shell step；不得用SQLite替代。

- [ ] **Step 5: 运行workflow syntax相关单测并提交**

Run:

```bash
cd backend
uv run pytest tests/test_postgres_fixture.py tests/test_check_script.py -q
```

Expected: PASS。

```bash
git add backend/tests/support/m5_automation.py backend/tests/integration/test_m5_project_automation_postgres.py .github/workflows/project-foundation-postgres-tests.yml
git commit -m "test: gate M5 automation isolation"
```

---

### Task 16: 建立真实 PostgreSQL migration、fresh install和fail-before-DDL门禁

**Files:**

- Create: `backend/tests/integration/test_m5_automation_migration_postgres.py`
- Modify: `backend/tests/support/m5_automation.py`
- Modify: `.github/workflows/project-foundation-postgres-tests.yml`

**Interfaces:**

- 同一测试文件建立两类临时数据库：`0011` legacy source和fresh empty database。
- Migration integration调用真实 CLI core、Alembic revisions和PostgreSQL constraints。
- 任何invalid map/fingerprint/source relation必须在`0013`首条DDL之前失败，并保留revision=`0012`。

- [ ] **Step 1: 写0011→0012→0013、幂等和empty install失败测试**

```python
async def test_legacy_migration_reaches_head_and_is_idempotent(m5_legacy_database):
    dry_run = await migrate_automations(
        m5_legacy_database.url,
        owner_map=m5_legacy_database.owner_map,
        backup_dir=m5_legacy_database.backup_dir,
        execute=False,
    )
    assert dry_run.cutover_complete is False
    assert dry_run.counts == m5_legacy_database.expected_counts

    executed = await migrate_automations(
        m5_legacy_database.url,
        owner_map=m5_legacy_database.owner_map,
        backup_dir=m5_legacy_database.backup_dir,
        execute=True,
    )
    assert executed.cutover_complete is True
    assert await current_revision(m5_legacy_database.url) == (
        "0013_project_automation_finalize"
    )

    repeated = await migrate_automations(
        m5_legacy_database.url,
        owner_map=m5_legacy_database.owner_map,
        backup_dir=m5_legacy_database.backup_dir,
        execute=True,
    )
    assert repeated.noop is True
```

Fresh install必须直接创建final schema、empty-domain marker和ready cutover state，不能要求owner map。

- [ ] **Step 2: 写map conflict、fingerprint变化和fail-before-DDL失败测试**

```python
async def test_finalize_probe_fails_before_ddl(m5_expand_database):
    await m5_expand_database.insert_unmapped_legacy_task()
    before = await m5_expand_database.schema_fingerprint()
    with pytest.raises(CommandError, match="automation finalize probe failed"):
        await m5_expand_database.upgrade("head")
    assert await m5_expand_database.current_revision() == (
        "0012_project_automation_expand"
    )
    assert await m5_expand_database.schema_fingerprint() == before
```

Fingerprint变化发生在dry-run与execute之间时必须拒绝；reuse Thread跨scope、owner map改变和ledger target digest冲突都不得写cutover marker。

- [ ] **Step 3: 运行新migration integration，证明门禁能捕获缺口**

Run:

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/integration/test_m5_automation_migration_postgres.py -q
```

Expected: FAIL，首个未满足的0011 migration、empty install、fingerprint或fail-before-DDL断言给出具体原因；不得以SKIP作为失败证据。

- [ ] **Step 4: 按失败类别完成对应实现并重跑两次**

失败只允许落到以下已定义边界：schema/empty finalize修改Task 1列出的两个revision与schema test；
map/fingerprint/ledger/idempotency修改Task 10的`migrate_automations.py`与migration unit tests；数据库创建/销毁问题只
修改`backend/tests/support/m5_automation.py`。每次先把integration failure下沉为对应unit failing test，再实现同一
contract，禁止在integration test里放宽断言。

Run:

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/integration/test_m5_automation_migration_postgres.py -q
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/integration/test_m5_automation_migration_postgres.py -q
```

Expected: 两次均PASS；failure cases保留可重跑状态且没有半成品final DDL。

- [ ] **Step 5: 将migration文件加入固定CI并运行M1–M5组合**

Workflow pytest列表追加：

```yaml
          tests/integration/test_m5_automation_migration_postgres.py
```

Run:

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest \
  tests/integration/test_m1_postgres_cutover.py \
  tests/integration/test_project_isolation_postgres.py \
  tests/integration/test_m2_project_governance_postgres.py \
  tests/integration/test_m3_shared_assets_postgres.py \
  tests/integration/test_m4_private_work_postgres.py \
  tests/integration/test_m4_private_work_migration_postgres.py \
  tests/integration/test_m5_project_automation_postgres.py \
  tests/integration/test_m5_automation_migration_postgres.py -q
```

Expected: PASS；M1–M4在Alembic head前进后仍通过。

- [ ] **Step 6: 提交migration gate**

```bash
git add backend/tests/integration/test_m5_automation_migration_postgres.py backend/tests/support/m5_automation.py .github/workflows/project-foundation-postgres-tests.yml
git commit -m "test: gate M5 automation migration"
```

---

### Task 17: 完成Frontend隔离/E2E门禁并开启PROJECT_AUTOMATION

**Files:**

- Modify: `frontend/src/core/projects/features.ts`
- Create: `frontend/tests/e2e/project-automations.spec.ts`
- Modify: `frontend/tests/e2e/utils/mock-api.ts`
- Modify: `frontend/tests/e2e/project-private-work-isolation.spec.ts`
- Modify: `frontend/tests/e2e/project-private-chat.spec.ts`
- Modify: `frontend/tests/unit/components/projects/project-automation-entry.test.tsx`
- Modify: `frontend/tests/unit/core/project-automations/hooks.test.tsx`

**Interfaces:**

- E2E mock API按auth account + project ID存Automation state，不使用全局task map。
- 覆盖create/edit/pause/resume/trigger/delete/history、Viewer、readiness、409/429/503和direct URL。
- Flag只在本任务所有unit/E2E isolation tests通过后改为 `true as const`。

- [ ] **Step 1: 写未开启flag下的完整E2E失败场景**

```typescript
test("keeps automation data isolated across account and project transitions", async ({
  page,
}) => {
  await mockProjectAutomationAPI(page, {
    accountId: ACCOUNT_A,
    projectId: PROJECT_A,
    tasks: [AUTOMATION_A],
  });
  await openProjectAutomations(page, "project-a");
  await expect(page.getByText(AUTOMATION_A.title)).toBeVisible();

  await switchAccountAndProject(page, ACCOUNT_B, "project-b");
  await expect(page.getByText(AUTOMATION_A.title)).not.toBeVisible();
  await expectNoAutomationRequestFor(page, ACCOUNT_A, PROJECT_A);
});
```

另写E2E验证：

- Viewer能打开list/history但没有mutation按钮；
- create/edit/pause/resume/manual/delete请求使用project URL；
- manual retry复用同一个`Idempotency-Key`；
- scheduler disabled仍允许manual；migration required不发list请求；
- 409刷新版本，429/503展示safe retry；
- direct URL无capability返回404/403；
- static demo没有入口、页面请求和legacy fallback；
- Project Chat link只跳project Automation route。

- [ ] **Step 2: 运行focused unit和E2E，确认flag关闭时失败**

Run:

```bash
cd frontend
pnpm test -- project-automations project-automation-entry project-chat
pnpm test:e2e -- project-automations.spec.ts project-private-work-isolation.spec.ts project-private-chat.spec.ts
```

Expected: 新E2E因入口关闭或mock handler未实现而FAIL。

- [ ] **Step 3: 实现account/project scoped mock和迟到响应断言**

Mock key固定：

```typescript
function automationStoreKey(accountId: string, projectId: string): string {
  return `${accountId}:${projectId}`;
}
```

Account/project切换测试挂起旧scope list response；切换时断言旧request收到abort，释放迟到响应后新scope cache仍不包含旧task。Mutation cache也必须由private-work scope transition先cancel后clear。

- [ ] **Step 4: 开启编译期入口并重跑focused gate**

```typescript
export const PROJECT_AUTOMATION = true as const;
```

Run:

```bash
cd frontend
pnpm test -- project-automations project-automation-entry project-chat
pnpm test:e2e -- project-automations.spec.ts project-private-work-isolation.spec.ts project-private-chat.spec.ts
```

Expected: PASS。

- [ ] **Step 5: 运行Frontend全量门禁并提交**

Run:

```bash
cd frontend
pnpm check
pnpm format
pnpm test
pnpm test:e2e
```

Expected: 全部PASS。

```bash
git add frontend/src/core/projects/features.ts frontend/tests/e2e/project-automations.spec.ts frontend/tests/e2e/utils/mock-api.ts frontend/tests/e2e/project-private-work-isolation.spec.ts frontend/tests/e2e/project-private-chat.spec.ts frontend/tests/unit/components/projects/project-automation-entry.test.tsx frontend/tests/unit/core/project-automations/hooks.test.tsx
git commit -m "test: release project automation frontend"
```

---

### Task 18: 同步文档、执行full gate并完成独立审查

**Files:**

- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `backend/AGENTS.md`
- Modify: `frontend/AGENTS.md`
- Modify: `docs/superpowers/specs/2026-07-12-project-first-saas-design.md`
- Modify: `docs/superpowers/specs/2026-07-14-project-private-work-m4-design.md`
- Modify: `docs/superpowers/specs/2026-07-16-project-automation-m5-design.md`
- Create: `docs/operations/m5-automation-migration.md`
- Modify: `CHANGELOG.md`

**Interfaces:**

- Runbook包含maintenance window、停止writer、外部认证backup证明、dry-run、execute、check-db、M1–M5 probes、失败恢复和回滚边界。
- M5 spec仅在fresh full gate和独立审查通过后改为“已完成”。
- 总体进度改为5/8（62.5%），继续明确系统不能作为完整多用户SaaS发布。

- [ ] **Step 1: 先写文档一致性失败测试/扫描**

Run:

```bash
if rg -n "M5.*尚未完成|M1.M2.M3.*M4 PostgreSQL|PROJECT_AUTOMATION.*false" \
  README.md README_zh.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md \
  docs/superpowers/specs config.example.yaml .github/workflows frontend/src/core/projects/features.ts; then
  exit 1
fi
```

Expected: FAIL并列出尚未更新的M5状态、旧CI名称或关闭flag，证明文档同步尚未完成。

- [ ] **Step 2: 编写用户、开发和运维文档**

README说明项目Automation入口、Viewer只读、manual trigger和scheduler config。三个AGENTS说明：

- project+owner是private authority；
- occurrence-before-admission和at-most-once replay边界；
- Scheduler嵌入single Gateway；
- M6才负责独立Worker、持久化SSE、通用jobs/retries；
- query key必须含account/project，scope切换cancel-before-clear。

Runbook必须给出exact命令：

```bash
make migrate-automations ARGS="--dry-run --owner-map /secure/m5-owner-map.json --backup-dir /secure/m5-backup-proof"
make migrate-automations ARGS="--execute --owner-map /secure/m5-owner-map.json --backup-dir /secure/m5-backup-proof"
make check-db
```

不得把prompt/title、owner map或完整ID写入示例日志。

- [ ] **Step 3: 运行Backend full verification**

Run:

```bash
cd backend
uv run pytest tests/ -q
make test-blocking-io
make lint
uvx ruff format --check .
```

Expected: 全部PASS且没有blocking-I/O finding。

- [ ] **Step 4: 运行真实PostgreSQL、运维和migration smoke**

Run:

```bash
cd backend
test -n "$POSTGRES_TEST_URL"
uv run pytest \
  tests/integration/test_m1_postgres_cutover.py \
  tests/integration/test_project_isolation_postgres.py \
  tests/integration/test_m2_project_governance_postgres.py \
  tests/integration/test_m3_shared_assets_postgres.py \
  tests/integration/test_m4_private_work_postgres.py \
  tests/integration/test_m4_private_work_migration_postgres.py \
  tests/integration/test_m5_project_automation_postgres.py \
  tests/integration/test_m5_automation_migration_postgres.py -q
cd ..
make doctor
make check-db
```

Expected: `test -n`成功且所有命令PASS。另用Task 16 fixtures各执行一次fresh install smoke和legacy dry-run/execute smoke；缺真实PostgreSQL证据不得标记M5完成。

- [ ] **Step 5: 运行Frontend full verification和workspace检查**

Run:

```bash
cd frontend
pnpm check
pnpm format
pnpm test
pnpm test:e2e
cd ..
git diff --check
rg -n "M5.*尚未完成|M1.M2.M3.*M4 PostgreSQL|PROJECT_AUTOMATION.*false" \
  README.md README_zh.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md \
  docs/superpowers/specs config.example.yaml .github/workflows frontend/src/core/projects/features.ts
```

Expected: Frontend命令PASS、`git diff --check`无输出；一致性扫描只允许M5前置历史描述，不允许当前状态、CI gate或flag仍旧。

- [ ] **Step 6: 请求一次独立代码审查并关闭findings**

使用 `superpowers:requesting-code-review`，审查范围从M5首个implementation commit到当前HEAD。Reviewer必须核对design第18节全部完成标准、scope predicates、crash replay边界、migration fail-before-DDL、client authority stripping和M6边界。

如发现Critical/Important：先使用 `superpowers:receiving-code-review` 验证finding，添加失败测试，修复并重跑Steps 3–5；每项记录修复commit。未关闭Critical/Important时不得继续。

- [ ] **Step 7: 标记M5完成并做最终fresh verification**

只有Steps 3–6均有本次运行的成功输出后，更新：

- M5 spec状态为“已完成”；
- 总体SaaS设计和root AGENTS为M1–M5完成（5/8，62.5%）；
- M6/M7/M8仍未完成，系统仍不可作为完整多用户SaaS发布。

然后再次运行受改动影响的文档扫描、`git diff --check`和M5 focused backend/frontend tests。

- [ ] **Step 8: 提交文档与完成标记**

```bash
git add README.md README_zh.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md docs/superpowers/specs/2026-07-12-project-first-saas-design.md docs/superpowers/specs/2026-07-14-project-private-work-m4-design.md docs/superpowers/specs/2026-07-16-project-automation-m5-design.md docs/operations/m5-automation-migration.md CHANGELOG.md
git commit -m "docs: complete M5 project automation"
```

---

## Plan Self-Review

需求覆盖映射：

| Design requirement | Implementation tasks |
| --- | --- |
| final schema、revision ancestry、M4 descendant compatibility | 1、3、15、16 |
| project+owner scoped definition/history | 2、4、9、15 |
| occurrence reservation、idempotency、claim/lease、global cap | 5、7、15 |
| M4 private Thread/run、exact Agent snapshot、non-interactive stripping | 6、9、15 |
| crash reconciliation不重放admitted run | 7、15 |
| revoke/downgrade/project lifecycle freeze | 8、15 |
| strict API、readiness和legacy fail-closed | 3、9、14 |
| explicit map、dry-run、ledger、fail-before-DDL migration | 1、10、16 |
| account/project cache和scope cleanup | 11、17 |
| project UI、Viewer、nav/chat/static gates | 12、13、17 |
| single-Gateway scheduler config和blocking-I/O | 7、14 |
| real PostgreSQL和Frontend release gates | 15、16、17 |
| docs、runbook、full verification和独立审查 | 18 |

执行纪律：每个task先运行指定失败测试，再写最小实现，再运行pass command并单独提交；Task 17之前保持产品flag关闭，Task 18之前不得声称M5完成。
