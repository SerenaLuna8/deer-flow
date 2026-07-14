# M4 Project Private Work Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不复制现有 LangGraph 运行栈的前提下，把 Thread、run、checkpoint、file、Memory 和 IM connection 改造成 `project_id + owner_user_id` 双重隔离的项目私有工作，并让每次 run 使用 M3 的精确资产快照。

**Architecture:** Gateway 从服务端 `ProjectContext` 派生不可变 `PrivateWorkContext`，所有私有业务通过强制 scope repository 和 `ProjectScopedCheckpointer` 进入现有 `RunManager/run_agent` 生命周期。PostgreSQL 成为 Thread metadata、run/event、file/artifact、Memory 和 connection 的权威；legacy 数据通过 expand → 显式迁移 → finalize → cutover marker 一次切换，不长期双写。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、SQLAlchemy 2 async、Alembic、asyncpg、PostgreSQL 17、LangGraph PostgreSQL checkpointer、pytest、Next.js 16 App Router、React 19、TypeScript、Zod、TanStack Query、LangGraph SDK、Playwright。

## Global Constraints

- 以 `docs/superpowers/specs/2026-07-14-project-private-work-m4-design.md` 为冻结规格；总体进度只在 Task 18 的全部门禁通过后更新为 4/8。
- 只使用 PostgreSQL；不增加 SQLite runtime，不修改 LangGraph 上游 checkpoint 表结构，不使用 PostgreSQL RLS。
- `PrivateWorkContext` 只能由服务端认证身份和数据库中的 active membership 派生；丢弃客户端 owner、role、capability、membership version、project context 和 `__private_*` 字段。
- 所有私有根资源必须同时带非空 `project_id` 和 `owner_user_id`；普通业务代码不得获得 unscoped repository 或裸 checkpointer。
- Thread ID 全局唯一；`threads_meta` 继续作为 Thread authority 表；`run_events(category='message')` 继续作为消息持久化，不新增第二份消息正文。
- M4 复用现有 `RunManager`、`run_agent`、stream bridge、goal、compact、branch、human-input 和前端 chat 组件，不建设第二套 runtime。
- run admission 同时要求 `private_work.create` 与 `shared_assets.execute`，并持久化 M3 exact Agent/Skill/MCP version、checksum、grant 和 catalog generation。
- credential 明文只允许在 M3 materializer 到当前 MCP 调用之间短暂存在；不得进入 run kwargs、event、checkpoint、file、Memory、日志或异常。
- PostgreSQL 是 file/artifact authority；沙箱目录只是一轮 run 的临时投影，上传、恢复、下载和 finalization 都按 chunk 流式处理。
- Viewer 只能读取、导出和删除自己的既有私有数据；不能创建/branch Thread、启动 run、上传、创建 connection 或修改 Memory。
- membership left/removed、降为 Viewer、project suspended/pending deletion 必须写数据库取消标记；本地 task cancel 只是 commit 后 best-effort 加速。
- legacy private API 保留到 M7，但 cutover marker 后统一返回 `409 PRIVATE_WORK_CUTOVER`，不得猜 default project 或读取 project-scoped rows。
- M4 使用 maintenance-window staged migration；owner map 必须显式，execute 前必须验证数据库备份证明和认证加密 filesystem backup。
- 后端和前端均按 TDD 实施；每个任务先运行目标失败测试，再写最小实现，再运行受影响测试并单独提交。
- 真实 PostgreSQL 测试只能创建随机 `deerflow_test_*` 数据库；本地缺 `POSTGRES_TEST_URL` 可明确 skip，CI 必须在 pytest 前硬失败。
- 实施前从最新 `dev` 创建 `codex/m4-private-work`；保留用户已有改动，不混入无关重构。

---

## Gate 与依赖顺序

1. **Gate 1 — schema 与 scope foundation：** Tasks 1–2。
2. **Gate 2 — Thread/run/checkpoint/asset snapshot：** Tasks 3–6。
3. **Gate 3 — file/Memory/connection authority：** Tasks 7–10。
4. **Gate 4 — API/frontend/cutover/release gate：** Tasks 11–18。

产品入口 `PROJECT_PRIVATE_WORKSPACE` 只能在 Task 17 的 M1–M4 PostgreSQL gate 和 Task 18 的完整验证通过后开启。

---

### Task 1: 建立 M4 final-state ORM 与 expand/finalize schema

**Files:**

- Create: `backend/packages/harness/deerflow/persistence/private_work/__init__.py`
- Create: `backend/packages/harness/deerflow/persistence/private_work/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/thread_meta/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/run/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/models/run_event.py`
- Modify: `backend/packages/harness/deerflow/persistence/feedback/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/channel_connections/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/models/__init__.py`
- Modify: `backend/packages/harness/deerflow/persistence/bootstrap.py`
- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/0008_project_private_work_expand.py`
- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/0009_project_private_work_finalize.py`
- Create: `backend/tests/test_m4_private_work_schema_postgres.py`
- Modify: `backend/tests/test_persistence_migrations_env.py`
- Modify: `backend/tests/test_default_project_bootstrap.py`

**Interfaces:**

- Produces final ORM rows: `RunAssetVersionRow`、`RunMcpGrantSnapshotRow`、`PrivateFileRow`、`PrivateFileChunkRow`、`PrivateArtifactRow`、`UserProjectMemoryRow`、`UserProjectMemoryFactRow`、可选 `UserProjectMemoryVectorRow`、`PrivateWorkMigrationRunRow`、`PrivateWorkMigrationLedgerRow`、`PrivateWorkCutoverStateRow`。
- `0008` 只增加 nullable scope/backfill columns、新表、索引、ledger/marker；`0009` 在任何 DDL 前验证 prerequisite，然后 rename `user_id -> owner_user_id` 并安装 final constraints。
- LangGraph-owned `checkpoints`、`checkpoint_blobs`、`checkpoint_writes` 继续被 migration env 排除。

- [ ] **Step 1: 写 final schema 与 staged revision 失败测试**

```python
M4_TABLES = {
    "run_asset_versions",
    "run_mcp_grant_snapshots",
    "files",
    "file_chunks",
    "artifacts",
    "user_project_memories",
    "user_project_memory_facts",
    "private_work_migration_runs",
    "private_work_migration_ledger",
    "private_work_cutover_state",
}

async def test_m4_finalize_schema_has_private_scope_and_composite_fks(
    migrated_postgres_database_url,
):
    engine = create_async_engine(migrated_postgres_database_url)
    async with engine.connect() as connection:
        inspector = await connection.run_sync(inspect)
        tables = set(inspector.get_table_names())
        thread_columns = {item["name"]: item for item in inspector.get_columns("threads_meta")}
        revision = (await connection.execute(
            text("SELECT version_num FROM alembic_version")
        )).scalar_one()
    assert M4_TABLES <= tables
    assert thread_columns["project_id"]["nullable"] is False
    assert thread_columns["owner_user_id"]["nullable"] is False
    assert "user_id" not in thread_columns
    assert revision == "0009_project_private_work_finalize"
```

同时覆盖：

- `threads_meta/runs/run_events/feedback/files/artifacts/memory/channel_*` 的 project-owner NOT NULL；
- project/user/membership 复合 FK；
- Thread、run、file、artifact、conversation 的完整父子复合 FK；
- active logical path partial unique；
- snapshot 不含 secret/envelope/key/locator 列；
- `0009` 在 legacy NULL、缺 migration prerequisite 或 probe 未完成时零 DDL 失败；
- fresh `Base.metadata.create_all` 产生与 `0009` 一致的 final schema；
- empty install 只有在确认没有 legacy private source并完成空域 probe后才写 `empty_install/cutover_complete` marker；
- migration env 仍不接管 LangGraph checkpoint 表。

- [ ] **Step 2: 运行测试确认缺少 M4 revision**

Run: `cd backend && uv run pytest tests/test_m4_private_work_schema_postgres.py tests/test_persistence_migrations_env.py tests/test_default_project_bootstrap.py -q`

Expected: FAIL，提示 revision 仍为 `0007_project_shared_assets` 或缺少 `files`/`project_id`；若真实 PostgreSQL fixture skip，先配置本地 test admin URL，不能把 skip 当作红灯证据。

- [ ] **Step 3: 建立 final-state ORM**

核心列按以下 final contract 定义；时间列统一 timezone-aware，UUID 用 `sa.Uuid()`，owner FK 长度与 `users.id` 一致为 36：

```python
class RunAssetVersionRow(Base):
    __tablename__ = "run_asset_versions"
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    dependency_order: Mapped[int] = mapped_column(primary_key=True)
    asset_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    catalog_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)

class PrivateFileChunkRow(Base):
    __tablename__ = "file_chunks"
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_index: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
```

`threads_meta` final columns为 `project_id`、`owner_user_id`、`agent_asset_id`、`agent_scope`、`frozen_at`、`deleted_at`、`checkpoint_delete_status`、`version`；`runs` final columns增加 project/owner、authorization cancel marker/reason、`finalization_status`。所有 snapshot/file/memory/channel 行安装规格中的 CHECK、unique 和 composite FK。

- [ ] **Step 4: 编写 expand revision**

`0008_project_private_work_expand` 必须：

```python
revision = "0008_project_private_work_expand"
down_revision = "0007_project_shared_assets"

def upgrade() -> None:
    for table in ("threads_meta", "runs", "run_events", "feedback"):
        op.add_column(table, sa.Column("project_id", sa.Uuid(), nullable=True))
    # owner_user_id rename 留给 0009；这里只建立可回填的 supporting indexes。
    _create_private_work_tables(nullable_scope=True)
    _create_migration_control_tables()
```

新业务表在 expand 阶段允许 scope NULL 仅供 migration staging；项目入口保持关闭。`private_work_cutover_state` 使用 singleton row，区分 `empty_install`、`migration_ready` 和 `cutover_complete`，不能用表是否存在推断 cutover。

`bootstrap.py` 在空数据库 `create_all + stamp head` 后执行只读 legacy-source probe；仅当数据库和 filesystem 都无 legacy private source时写 empty-install marker。已有/versioned数据库永不在普通 startup中猜 owner map或自动写 marker。

当 versioned数据库位于 `0007` 或更早且检测到 legacy private source时，普通 Gateway bootstrap必须在执行 `upgrade head` 前失败并提示运行 `make migrate-private-work`；不能由启动过程偷偷应用 `0008/0009`。显式迁移脚本是跨越M4 staged boundary的唯一入口。

- [ ] **Step 5: 编写 fail-before-DDL finalize revision**

`0009` 的第一条动作调用纯验证 SQL；任何失败都在 `op.alter_column` 前抛出：

```python
def upgrade() -> None:
    bind = op.get_bind()
    _assert_finalize_prerequisites(bind)
    _rename_owner_columns()
    _make_private_scope_not_null()
    _install_scope_checks_uniques_and_composite_foreign_keys()
```

prerequisite 要求一个 completed migration run、全部 domain ledger/probe 为 complete、所有待收紧列无 NULL、checkpoint marker coverage 完整。cutover marker 不作为前置；由 Task 13 在 `0009` 成功后写入。

- [ ] **Step 6: 跑 schema、upgrade/downgrade 安全测试**

Run: `cd backend && uv run pytest tests/test_m4_private_work_schema_postgres.py tests/test_project_schema_postgres.py tests/test_project_governance_schema_postgres.py tests/test_m3_shared_assets_schema_postgres.py tests/test_persistence_migrations_env.py tests/test_default_project_bootstrap.py -q`

Expected: PASS；`0009` downgrade 在 M4 表有数据时 fail closed，空库 downgrade 可回到 `0008`，再回到 `0007`。

- [ ] **Step 7: 提交 Gate 1 schema**

```bash
git add backend/packages/harness/deerflow/persistence backend/tests/test_m4_private_work_schema_postgres.py backend/tests/test_persistence_migrations_env.py backend/tests/test_default_project_bootstrap.py
git commit -m "feat: add M4 private work schema"
```

---

### Task 2: 建立可信 PrivateWorkContext、稳定错误和 harness scope value

**Files:**

- Create: `backend/packages/harness/deerflow/runtime/private_scope.py`
- Modify: `backend/packages/harness/deerflow/runtime/__init__.py`
- Create: `backend/app/private_work/__init__.py`
- Create: `backend/app/private_work/context.py`
- Create: `backend/app/private_work/errors.py`
- Create: `backend/app/private_work/error_mapping.py`
- Create: `backend/app/private_work/revalidation.py`
- Modify: `backend/app/projects/context.py`
- Create: `backend/tests/test_private_work_context.py`
- Create: `backend/tests/test_private_work_error_mapping.py`
- Create: `backend/tests/test_private_work_import_firewall.py`

**Interfaces:**

- `PrivateWorkContext.from_project(ProjectContext)` 是唯一构造入口。
- harness 只认识不含 role/capability 的 `PrivateResourceScope` protocol/value；不得 import `app.projects` 或 `app.private_work`。
- `PrivateWorkRevalidator.require(context, *capabilities)` 在每个 mutation/side-effect boundary 重新读取 project + membership/version。

- [ ] **Step 1: 写 context/client stripping/revalidation 失败测试**

```python
def test_private_work_context_can_only_derive_from_project_context(project_context):
    context = PrivateWorkContext.from_project(project_context)
    assert context.project_id == project_context.project_id
    assert context.user_id == project_context.user_id
    assert context.resource_scope == PrivateResourceScope(
        project_id=str(project_context.project_id),
        owner_user_id=str(project_context.user_id),
        membership_version=project_context.membership_version,
    )

def test_strip_private_client_fields_drops_all_authority_fields():
    cleaned = strip_private_client_fields({
        "project_id": "attacker",
        "owner_user_id": "attacker",
        "role": "admin",
        "capabilities": ["shared_assets.execute"],
        "membership_version": 999,
        "project_context": {},
        "__private_scope": {},
        "model_name": "allowed-model",
    })
    assert cleaned == {"model_name": "allowed-model"}
```

再测试 stale membership、suspended project、left membership 返回 not-found；同 scope 但缺 capability 返回 forbidden；client-shaped dict 不能传入 M3 resolver/materializer。

- [ ] **Step 2: 运行测试确认模块缺失**

Run: `cd backend && uv run pytest tests/test_private_work_context.py tests/test_private_work_error_mapping.py tests/test_private_work_import_firewall.py -q`

Expected: FAIL，提示 `app.private_work` 或 `PrivateResourceScope` 不存在。

- [ ] **Step 3: 定义 harness-safe scope 与 app context**

```python
@dataclass(frozen=True, slots=True)
class PrivateResourceScope:
    project_id: str
    owner_user_id: str
    membership_version: int

@dataclass(frozen=True, slots=True)
class PrivateWorkContext:
    user_id: uuid.UUID
    project_id: uuid.UUID
    membership_id: uuid.UUID
    role: ProjectRole
    capabilities: frozenset[Capability]
    membership_version: int
    request_id: str

    @classmethod
    def from_project(cls, context: ProjectContext) -> "PrivateWorkContext":
        if type(context) is not ProjectContext:
            raise PrivateWorkNotFound(getattr(context, "request_id", "unknown"))
        return cls(**dataclasses.asdict(context))

    @property
    def resource_scope(self) -> PrivateResourceScope:
        return PrivateResourceScope(
            str(self.project_id), str(self.user_id), self.membership_version
        )
```

禁止公开接收 `(project_id, owner_user_id)` 的便捷构造器；测试扫描 harness import graph，确保 `backend/packages/harness` 不依赖 `app.*`。

- [ ] **Step 4: 定义稳定 domain errors 与 HTTP mapper**

使用以下公共 code/status：

```python
PRIVATE_WORK_ERROR_STATUS = {
    "PRIVATE_WORK_NOT_FOUND": 404,
    "PRIVATE_WORK_FORBIDDEN": 403,
    "PRIVATE_WORK_CONFLICT": 409,
    "PRIVATE_WORK_ASSET_STALE": 409,
    "PRIVATE_WORK_CUTOVER": 409,
    "PRIVATE_WORK_TOO_LARGE": 413,
    "PRIVATE_WORK_INVALID": 422,
    "PRIVATE_WORK_UNAVAILABLE": 503,
}
```

response detail 只包含 `code`、固定 public message、`request_id`。mapper 不拼接 exception、SQL、资源 ID、文件名、provider error 或 credential metadata。

- [ ] **Step 5: 实现事务内 revalidator**

```python
class PrivateWorkRevalidator:
    async def require(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *capabilities: Capability,
        lock: bool = False,
    ) -> ProjectContext:
        current = await resolve_project_context_in_transaction(
            session,
            context.user_id,
            context.project_id,
            context.request_id,
            lock=lock,
        )
        if current.membership_id != context.membership_id:
            raise PrivateWorkNotFound(context.request_id)
        if current.membership_version != context.membership_version:
            raise PrivateWorkNotFound(context.request_id)
        for capability in capabilities:
            if capability not in current.capabilities:
                raise PrivateWorkForbidden(context.request_id)
        return current
```

`resolve_project_context_in_transaction` 不自行 `session.begin()`，供已有 transaction 的 mutation/side-effect boundary 使用；现有 HTTP wrapper `resolve_project_context` 保留自身 transaction wrapper。`lock=True` 使用同一 joined query 的 `FOR UPDATE` 版本，遵守 project → membership 锁序，禁止在已有 transaction 内嵌套调用当前会自行 begin 的 resolver。

- [ ] **Step 6: 跑 context/error/import firewall 测试并提交**

Run: `cd backend && uv run pytest tests/test_private_work_context.py tests/test_private_work_error_mapping.py tests/test_private_work_import_firewall.py tests/test_project_context.py tests/test_project_capabilities.py -q`

```bash
git add backend/packages/harness/deerflow/runtime backend/app/private_work backend/tests/test_private_work_*.py
git commit -m "feat: add trusted private work context"
```

---

### Task 3: 将 Thread authority 与 checkpoint 访问改成强制双重作用域

**Files:**

- Modify: `backend/packages/harness/deerflow/persistence/thread_meta/base.py`
- Modify: `backend/packages/harness/deerflow/persistence/thread_meta/sql.py`
- Modify: `backend/packages/harness/deerflow/persistence/thread_meta/memory.py`
- Create: `backend/app/private_work/thread_repository.py`
- Create: `backend/app/private_work/checkpointer.py`
- Create: `backend/app/private_work/thread_service.py`
- Modify: `backend/app/gateway/deps.py`
- Create: `backend/tests/test_private_thread_repository.py`
- Create: `backend/tests/test_project_scoped_checkpointer.py`
- Create: `backend/tests/test_private_thread_service.py`
- Modify: `backend/tests/test_thread_meta_repo.py`

**Interfaces:**

- `ThreadMetaStore` 的 project path 接受 `PrivateResourceScope`；legacy bypass 只能通过显式 `TrustedUnscopedThreadMetaStore` 运维适配器。
- `ProjectScopedCheckpointer` 覆盖 sync/async get/list/put/put_writes/delete_thread，并在每次调用前验证 scoped Thread。
- `PrivateThreadService` 负责 create/search/get/patch/delete/branch 的 authority transaction；route 不直接碰 repository/checkpointer。

- [ ] **Step 1: 写 Thread 跨 scope 与 raw saver 拒绝测试**

```python
async def test_private_thread_repository_never_returns_same_project_other_owner(repo):
    await repo.create(scope=OWNER_A, thread_id=THREAD_ID, agent=AGENT_REF)
    assert await repo.get(scope=OWNER_B, thread_id=THREAD_ID) is None
    assert await repo.get(scope=PROJECT_B_OWNER_A, thread_id=THREAD_ID) is None

async def test_scoped_checkpointer_rejects_marker_mismatch(wrapper, raw_saver):
    await raw_saver.aput(
        config(THREAD_ID), empty_checkpoint(),
        {"deerflow_private_scope": {"project_id": str(PROJECT_B), "owner_user_id": OWNER_A}},
        {},
    )
    with pytest.raises(PrivateWorkNotFound):
        await wrapper.for_context(CONTEXT_A).aget_tuple(config(THREAD_ID))
```

覆盖：缺 marker、伪造 marker、跨 owner/project、global thread UUID collision、deleted/frozen Thread、client configurable 伪造 scope、goal/compact/branch/state update/delete 的 raw-saver bypass。

- [ ] **Step 2: 运行测试确认现有 repo 只有 user scope**

Run: `cd backend && uv run pytest tests/test_private_thread_repository.py tests/test_project_scoped_checkpointer.py tests/test_private_thread_service.py -q`

Expected: FAIL，现有 `ThreadMetaStore` 不接受 `scope` 且 checkpointer 未包装。

- [ ] **Step 3: 让所有 Thread SQL 在数据库条件中包含完整 scope**

```python
stmt = select(ThreadMetaRow).where(
    ThreadMetaRow.thread_id == thread_id,
    ThreadMetaRow.project_id == uuid.UUID(scope.project_id),
    ThreadMetaRow.owner_user_id == scope.owner_user_id,
    ThreadMetaRow.deleted_at.is_(None),
)
```

create/search/update/delete/check_access 都使用相同 predicate，禁止 `session.get(ThreadMetaRow, thread_id)` 后 Python 判断。create 同时写 logical `agent_asset_id/agent_scope` 和 `version=1`；mutation 使用 version 条件更新，stale 变成 stable 409。

- [ ] **Step 4: 实现 scoped checkpointer view**

```python
class ProjectScopedCheckpointer:
    def __init__(self, raw, thread_repository, revalidator):
        self._raw = raw
        self._threads = thread_repository
        self._revalidator = revalidator

    def for_context(self, context: PrivateWorkContext):
        return _ScopedSaver(self, context)

class _ScopedSaver:
    async def aget_tuple(self, config):
        thread = await self._owner._threads.require(
            self._context, _server_thread_id(config)
        )
        value = await self._owner._raw.aget_tuple(_sanitize_config(config))
        _require_scope_marker(value, thread, self._context)
        return value
```

`aput` 覆盖 client marker，写 `deerflow_private_scope={project_id, owner_user_id}`；`alist` 每项校验 marker；`adelete_thread` 先将 Thread 标记不可见，再删除 checkpoint，失败时保留 `checkpoint_delete_status='retry_required'`。

- [ ] **Step 5: 实现原子 Thread create/delete/branch**

create 顺序固定为 project/membership lock → capability → Agent 可执行性 → Thread row → root checkpoint；root checkpoint 失败补偿删除 row。branch 只复制同 scope PostgreSQL authority files（Task 8 接入后完成 copy hook），不读取宿主目录。

- [ ] **Step 6: Gateway 只暴露 scoped saver factory**

`langgraph_runtime` 保留 raw saver 在私有 app-state 名称 `_raw_checkpointer`，`get_checkpointer` 继续仅供 legacy path；新增：

```python
def get_project_checkpointer(request: Request, context: PrivateWorkContext):
    return request.app.state.project_scoped_checkpointer.for_context(context)
```

Task 11 的项目 router、run service、goal/compact/branch/state mutation 只能依赖该 factory。测试通过 AST/import patch 证明项目模块从未调用 `get_checkpointer`。

- [ ] **Step 7: 跑 Thread/checkpoint 回归并提交**

Run: `cd backend && uv run pytest tests/test_private_thread_repository.py tests/test_project_scoped_checkpointer.py tests/test_private_thread_service.py tests/test_thread_meta_repo.py tests/test_threads_router.py tests/test_goal_runtime.py tests/test_thread_state_promoted.py -q`

```bash
git add backend/packages/harness/deerflow/persistence/thread_meta backend/app/private_work backend/app/gateway/deps.py backend/tests
git commit -m "feat: scope project threads and checkpoints"
```

---

### Task 4: 将 run、event、feedback 与 exact asset snapshot 纳入同一 private scope

**Files:**

- Modify: `backend/packages/harness/deerflow/runtime/runs/store/base.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/store/memory.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/manager.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Modify: `backend/packages/harness/deerflow/persistence/run/sql.py`
- Modify: `backend/packages/harness/deerflow/runtime/events/store/base.py`
- Modify: `backend/packages/harness/deerflow/runtime/events/store/db.py`
- Modify: `backend/packages/harness/deerflow/persistence/feedback/sql.py`
- Create: `backend/app/private_work/run_repository.py`
- Create: `backend/app/private_work/snapshot_repository.py`
- Create: `backend/tests/test_private_run_repository.py`
- Create: `backend/tests/test_private_run_snapshot.py`
- Modify: `backend/tests/test_run_manager.py`
- Modify: `backend/tests/test_run_repository.py`
- Modify: `backend/tests/test_run_event_store.py`
- Modify: `backend/tests/test_thread_messages_feedback.py`

**Interfaces:**

- `RunRecord` 增加 `scope: PrivateResourceScope | None`；项目 run 必须非空，legacy 仅在 cutover 前允许 `None`。
- RunStore/EventStore/Feedback project entrypoint 必须使用完整 scope；background update 通过 run row 自身 scope 条件更新，不接受客户端 owner。
- `RunSnapshotRepository.create_run_with_snapshot(context, thread, request, resolved_agent)` 在一个 transaction 写 run + asset closure + grant snapshot。

- [ ] **Step 1: 写 scope、FK、snapshot secret-zero 失败测试**

```python
async def test_run_snapshot_is_exact_and_secret_free(snapshot_repo, resolved_agent):
    run = await snapshot_repo.create_run_with_snapshot(
        CONTEXT_A, THREAD_ID, resolved_agent
    )
    assets = await snapshot_repo.list_assets(CONTEXT_A, run.run_id)
    assert [(row.asset_kind, row.version_id, row.payload_checksum)
            for row in assets] == EXPECTED_CLOSURE
    serialized = json.dumps([dataclasses.asdict(row) for row in assets])
    assert "secret" not in serialized.lower()
    assert "cipher" not in serialized.lower()
    assert "key_id" not in serialized.lower()
```

再覆盖同 project 另一 owner、另一 project、UUID 猜测、分页、update/delete、event seq、feedback、消息 history；数据库复合 FK 必须拒绝把 event/file snapshot 挂到错误 run。

- [ ] **Step 2: 运行测试确认当前 stores 只有 `user_id`**

Run: `cd backend && uv run pytest tests/test_private_run_repository.py tests/test_private_run_snapshot.py -q`

Expected: FAIL，`RunRecord` 缺 scope，snapshot repository/table 尚未接入。

- [ ] **Step 3: 演进 RunManager/RunStore，不复制 manager**

```python
record = RunRecord(
    run_id=run_id,
    thread_id=thread_id,
    assistant_id=assistant_id,
    scope=scope,
    status=RunStatus.pending,
    on_disconnect=on_disconnect,
    metadata=metadata or {},
    kwargs=kwargs or {},
    created_at=now,
    updated_at=now,
)
```

`create/create_or_reject/get/list_by_thread/cancel/update_*` 对项目 run 透传 `scope`。RunManager 的 in-memory lookup 在命中后也必须比较 scope，不能因为 memory hit 绕开 store 过滤；startup reconciliation 使用显式 trusted-unscoped store 方法，不复用产品方法。

- [ ] **Step 4: 收紧 SQL repository、event store 与 feedback**

项目 SQL 统一形态：

```python
where_scope = (
    RunRow.project_id == uuid.UUID(scope.project_id),
    RunRow.owner_user_id == scope.owner_user_id,
)
stmt = select(RunRow).where(RunRow.run_id == run_id, *where_scope)
```

event `put/put_batch/list` 从对应 scoped run/thread 获取 project-owner，禁止 event payload 提供 scope；feedback create/get/delete 同样通过完整 parent scope。保留 `user_id` 仅作为 cutover 前 adapter 参数，不让项目 router 调用。

- [ ] **Step 5: 实现 snapshot transaction**

`ResolvedAgentSnapshot` 的 root Agent 为 dependency order 0，随后按 resolver 返回的稳定 Skill/MCP dependency order 写 `run_asset_versions`。每个 MCP grant 写 `run_mcp_grant_snapshots(mcp_version_id, credential_slot_id, credential_grant_id, credential_version_id)`；repository API 不接受 secret-bearing对象，只接受 M3 安全 snapshot 与 closure IDs。

- [ ] **Step 6: 跑 run/event/feedback 回归并提交**

Run: `cd backend && uv run pytest tests/test_private_run_repository.py tests/test_private_run_snapshot.py tests/test_run_manager.py tests/test_run_repository.py tests/test_run_event_store.py tests/test_run_events_endpoint.py tests/test_thread_messages_feedback.py -q`

```bash
git add backend/packages/harness/deerflow/runtime backend/packages/harness/deerflow/persistence backend/app/private_work backend/tests
git commit -m "feat: scope runs and persist asset snapshots"
```

---

### Task 5: 建立项目 run admission 与 M3 exact runtime materialization

**Files:**

- Create: `backend/app/private_work/run_admission.py`
- Create: `backend/app/private_work/asset_runtime.py`
- Create: `backend/app/private_work/runtime_context.py`
- Modify: `backend/app/gateway/services.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`
- Create: `backend/tests/test_private_run_admission.py`
- Create: `backend/tests/test_private_asset_runtime.py`
- Create: `backend/tests/test_private_runtime_context.py`
- Modify: `backend/tests/test_legacy_system_asset_runtime.py`

**Interfaces:**

- `PrivateRunAdmissionService.admit(context, thread_id, request) -> AdmittedPrivateRun`。
- `PrivateAssetRuntime.materialize(context, admitted) -> PrivateAgentRuntime` 只从 persisted exact snapshot 构建 Agent/Skill/MCP。
- harness `RunContext` 接收 opaque `private_scope`、authorization checker、file authority hooks，不 import app domain。

- [ ] **Step 1: 写 admission 锁序、generation stale 和 client field stripping 测试**

```python
async def test_admission_persists_snapshot_before_starting_graph(service):
    admitted = await service.admit(CONTEXT_A, THREAD_ID, RUN_REQUEST)
    assert admitted.run.status == "pending"
    assert admitted.snapshot.catalog_generation == RESOLVED_GENERATION
    assert graph_factory.calls == []

async def test_catalog_change_between_snapshot_and_materialize_fails_closed(service):
    resolver.bump_generation_after_resolve()
    with pytest.raises(PrivateWorkAssetStale):
        await service.admit(CONTEXT_A, THREAD_ID, RUN_REQUEST)
    assert model.calls == []
    assert mcp.calls == []
```

覆盖 capability 双要求、Thread busy、Agent 不可执行、MCP grant revoke、project Skill 临时物化、legacy path 拒绝 project asset、client context/configurable 伪造字段被覆盖。

- [ ] **Step 2: 运行测试确认现有 `start_run` 直接 resolve global agent factory**

Run: `cd backend && uv run pytest tests/test_private_run_admission.py tests/test_private_asset_runtime.py tests/test_private_runtime_context.py -q`

Expected: FAIL，项目 admission service 不存在；现有 `start_run` 仍从 `assistant_id` 选择 legacy factory。

- [ ] **Step 3: 实现固定 admission transaction 与锁序**

```python
async def admit(self, context, thread_id, request):
    async with self._session_factory() as session, session.begin():
        current = await self._revalidator.require(
            session,
            context,
            Capability.PRIVATE_WORK_CREATE,
            Capability.SHARED_ASSETS_EXECUTE,
            lock=True,
        )
        thread = await self._threads.lock(session, current, thread_id)
        await self._runs.require_no_conflicting_active_run(session, current, thread)
        resolved = await self._resolver.resolve_project_asset_snapshot(
            current, AssetSelection(AssetKind.AGENT, thread.agent_asset_id)
        )
        admitted = await self._snapshots.insert(session, current, thread, resolved, request)
    return admitted
```

M3 resolver 当前自管 session；实现时增加可在 caller transaction 使用的 session-bound variant，或让 snapshot transaction 在 resolver 返回后重新锁 project/membership/thread 并重验 generation。不得持有跨 await 的未知 lock 顺序。

- [ ] **Step 4: 构建 exact runtime，禁止全局 cache 污染**

`PrivateAssetRuntime` 从已持久化 snapshot 重新加载 exact M3 versions；Skill 解包到 run-scoped temp dir；MCP definition 为每次 run 独立 mapping。调用 `materialize_mcp_secrets` 前比较 persisted/current generation 和 grant closure，返回的 `MaterializedMcpSecrets` 只保存在局部变量，MCP 调用结束立即释放引用。

- [ ] **Step 5: 扩展 runtime context 与 server overwrite**

```python
config["context"]["private_scope"] = admitted.opaque_runtime_scope
config["configurable"]["thread_id"] = admitted.thread_id
for key in PRIVATE_CLIENT_KEYS:
    client_context.pop(key, None)
    client_configurable.pop(key, None)
```

不把 project/owner/capability object写入 checkpointed configurable；checkpoint 只由 scoped saver 写安全 marker。worker 通过 `RunContext` 中的 opaque hooks 获取 revalidation/file authority。

- [ ] **Step 6: 将项目 path 接入现有 RunManager/run_agent**

新增 `start_private_run`，复用现有 normalize input、stream modes、RunManager record、stream bridge、`run_agent` 和 completion path；legacy `start_run` 保留到 Task 12 cutover guard。不得复制 `run_agent` 主循环。

- [ ] **Step 7: 跑 asset/runtime/legacy 回归并提交**

Run: `cd backend && uv run pytest tests/test_private_run_admission.py tests/test_private_asset_runtime.py tests/test_private_runtime_context.py tests/test_legacy_system_asset_runtime.py tests/test_runtime_lifecycle_e2e.py tests/test_runtime_channel_config_merge.py -q`

```bash
git add backend/app/private_work backend/app/gateway/services.py backend/packages/harness/deerflow backend/tests
git commit -m "feat: admit project runs with exact M3 assets"
```

---

### Task 6: 在授权撤销和每个副作用边界 fail-close 活动 run

**Files:**

- Create: `backend/app/private_work/authorization.py`
- Create: `backend/app/private_work/retention.py`
- Modify: `backend/app/projects/membership_service.py`
- Modify: `backend/app/projects/lifecycle_service.py`
- Modify: `backend/app/projects/membership_repository.py`
- Modify: `backend/app/projects/lifecycle_repository.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py`
- Modify: `backend/packages/harness/deerflow/sandbox/sandbox.py`
- Create: `backend/tests/test_private_run_authorization.py`
- Modify: `backend/tests/test_project_membership_service.py`
- Modify: `backend/tests/test_project_lifecycle_service.py`

**Interfaces:**

- `PrivateRunAuthorizationService.mark_revoked(session, project_id, owner_user_id, reason) -> tuple[str, ...]` 在治理 transaction 内写 marker。
- `PrivateWorkRetentionService.freeze_owner/restore_owner` 维护 `frozen_at` 与 connection frozen状态，但不物理删除 Thread、file、Memory或credential。
- `notify_local_cancellation(run_ids)` 只在 commit 后调用 RunManager.cancel。
- harness 接收 `AuthorizationBoundary` protocol，在 model、tool、MCP、sandbox side effect、file finalization 前调用。

- [ ] **Step 1: 写 downgrade/remove/suspend 与跨 worker 测试**

```python
async def test_remove_member_commits_cancel_marker_before_local_notify(service):
    await service.remove_member(ADMIN_CONTEXT, MEMBER_ID, version=3)
    run = await trusted_runs.get(RUN_ID)
    assert run.authorization_cancel_reason == "authorization_revoked"
    assert run.authorization_cancel_requested_at is not None
    assert notifier.commit_was_visible is True

async def test_remote_worker_stops_at_next_tool_boundary(boundary):
    await trusted_runs.mark_authorization_revoked(RUN_ID)
    with pytest.raises(AuthorizationRevoked):
        await boundary.before_tool_call(PRIVATE_RUNTIME_SCOPE)
```

覆盖 Admin→Editor/Runner 不取消，Admin/Editor/Runner→Viewer 取消，left/removed、project suspend/pending deletion 取消；left/remove冻结 private rows，原 membership row重新 active后恢复访问；已成功 run 不改终态；未知本地 task 不影响 DB marker；M4 永不执行 retention 到期物理清理。

- [ ] **Step 2: 运行测试确认治理 service 尚未联动 runs**

Run: `cd backend && uv run pytest tests/test_private_run_authorization.py tests/test_project_membership_service.py tests/test_project_lifecycle_service.py -q`

Expected: FAIL，run marker 未写入或 worker 不检查。

- [ ] **Step 3: 在同一治理 transaction 写 marker**

membership/lifecycle repository 按 project → membership → active runs 锁序调用：

```python
run_ids = await authorization.mark_revoked(
    session,
    project_id=project.id,
    owner_user_id=membership.user_id,
    reason="authorization_revoked",
)
```

service commit 后调用 notifier；不能让 notifier 失败回滚治理变更。

同一 transaction 调用 retention service：left/removed/suspend为 owner Thread写 `frozen_at`并冻结connection；rejoin/restore只清除属于同一 membership/project的 Thread freeze。file/Memory内容保持不变并始终由membership gate保护。

- [ ] **Step 4: 安装 runtime boundaries**

在每次 model invocation、tool/MCP dispatch、sandbox write/exec 和 Task 8 file finalization 前查询 run marker与当前 membership。发现失权：设置 abort event、抛出安全内部异常、run 终态 `interrupted`，public reason 仅 `authorization_revoked`。

- [ ] **Step 5: 跑治理/runtime 回归并提交 Gate 2**

Run: `cd backend && uv run pytest tests/test_private_run_authorization.py tests/test_project_membership_service.py tests/test_project_lifecycle_service.py tests/test_run_manager.py tests/test_runtime_lifecycle_e2e.py -q`

```bash
git add backend/app/private_work backend/app/projects backend/packages/harness/deerflow backend/tests
git commit -m "feat: stop runs when project authorization is revoked"
```

---

### Task 7: 建立 PostgreSQL chunked file/artifact authority

**Files:**

- Create: `backend/packages/harness/deerflow/persistence/private_work/file_repository.py`
- Create: `backend/app/private_work/file_paths.py`
- Create: `backend/app/private_work/file_service.py`
- Create: `backend/app/private_work/file_streaming.py`
- Modify: `backend/app/gateway/routers/uploads.py`
- Modify: `backend/app/gateway/routers/artifacts.py`
- Create: `backend/tests/test_private_file_repository.py`
- Create: `backend/tests/test_private_file_service.py`
- Create: `backend/tests/test_private_file_streaming.py`
- Modify: `backend/tests/test_uploads_router.py`
- Modify: `backend/tests/test_artifacts_router.py`
- Modify: `backend/tests/blocking_io/test_uploads_router.py`
- Modify: `backend/tests/blocking_io/test_artifacts_router.py`

**Interfaces:**

- `PrivateFileService.stage_upload(context, thread_id, logical_path, media_type) -> StagedFile`。
- `append_chunk/finalize_upload/abort_upload` 形成明确 staging lifecycle；默认 `PRIVATE_FILE_CHUNK_SIZE = 1 MiB`。
- `stream_file/stream_artifact` 返回 async chunk iterator；不得 `read_bytes()` 整文件。

- [ ] **Step 1: 写 path、chunk/hash、cleanup 和内存边界失败测试**

```python
@pytest.mark.parametrize("path", [
    "/etc/passwd", "../secret", "a/../../b", "C:/secret", "a\x00b", "a\\..\\b",
])
def test_private_file_path_rejects_escape(path):
    with pytest.raises(PrivateWorkInvalid):
        normalize_private_logical_path(path)

async def test_upload_streams_chunks_and_commits_ready_only_after_hash(service):
    result = await service.upload(
        CONTEXT_A, THREAD_ID, "uploads/report.pdf", chunk_source(TWO_AND_HALF_MIB)
    )
    assert result.status == "ready"
    assert await repo.chunk_sizes(result.id) == [MIB, MIB, HALF_MIB]
    assert result.sha256 == hashlib.sha256(TWO_AND_HALF_MIB).hexdigest()
```

覆盖多文件 total limit、request cancel、conversion failure、DB failure、chunk tamper、whole-file tamper、staging row cleanup、same path version/partial unique、same owner cross-project、same project cross-owner、下载 backpressure、active content attachment header。

- [ ] **Step 2: 运行测试确认当前 authority 是宿主目录**

Run: `cd backend && uv run pytest tests/test_private_file_repository.py tests/test_private_file_service.py tests/test_private_file_streaming.py -q`

Expected: FAIL，private file service 不存在；现有 upload router 直接写 `get_uploads_dir()`。

- [ ] **Step 3: 实现严格 logical path 与 scoped repository**

```python
def normalize_private_logical_path(raw: str) -> str:
    if not raw or "\x00" in raw or PureWindowsPath(raw).drive:
        raise PrivateWorkInvalid()
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise PrivateWorkInvalid()
    return path.as_posix()
```

repository 所有查询均带 project/owner/thread；`file_chunks` 按 `(file_id, chunk_index)` 递增写入，每次校验 `size == len(content)` 和 chunk SHA-256。文件 whole hash、size、status 与 chunks 在 finalize transaction 内一致。

- [ ] **Step 4: 实现 staging upload 与转换产物**

HTTP body 逐块读；每个输入文件先建 staging row，再写 chunks，达到 single/total/count limit 立即 abort。文档转换通过受控临时文件消费 chunk stream，转换结果写独立 `kind='workspace'` file row，并以 `source_file_id` 安全关联，不覆盖原文件。

- [ ] **Step 5: 实现 chunked download/artifact response**

```python
return StreamingResponse(
    file_service.stream_ready_file(context, file_id),
    media_type=file.media_type,
    headers=safe_download_headers(file.display_name),
)
```

先 scoped lookup 再 streaming；iterator 每次从数据库读取有界 chunk page。active MIME 强制 attachment；404/503 使用 Task 2 mapper，不回显 logical path。

- [ ] **Step 6: 保留 legacy router，项目 service 通过 Task 11 挂载**

现有 `/api/threads/{thread_id}/uploads` 与 `/api/artifacts` 在 cutover 前仍处理 legacy source；抽取 shared Pydantic response 和 limit helper，但不要让项目 path 调用宿主目录 helper。Task 12 再加 marker guard。

- [ ] **Step 7: 跑 file/blocking-I/O 回归并提交**

Run: `cd backend && uv run pytest tests/test_private_file_repository.py tests/test_private_file_service.py tests/test_private_file_streaming.py tests/test_uploads_router.py tests/test_artifacts_router.py tests/blocking_io/test_uploads_router.py tests/blocking_io/test_artifacts_router.py -q`

```bash
git add backend/packages/harness/deerflow/persistence/private_work backend/app/private_work backend/app/gateway/routers/uploads.py backend/app/gateway/routers/artifacts.py backend/tests
git commit -m "feat: make PostgreSQL authoritative for private files"
```

---

### Task 8: 将 sandbox restore、workspace finalization 和 branch 接到 file authority

**Files:**

- Create: `backend/app/private_work/sandbox_files.py`
- Create: `backend/app/private_work/file_finalizer.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/thread_data_middleware.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py`
- Modify: `backend/packages/harness/deerflow/workspace_changes/recorder.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Modify: `backend/app/private_work/thread_service.py`
- Create: `backend/tests/test_private_sandbox_files.py`
- Create: `backend/tests/test_private_file_finalizer.py`
- Modify: `backend/tests/test_uploads_middleware_core_logic.py`
- Modify: `backend/tests/test_thread_data_middleware.py`
- Modify: `backend/tests/test_run_worker_rollback.py`

**Interfaces:**

- `PrivateSandboxFileProjection.restore(run_scope, sandbox) -> AuthorityManifest`。
- `PrivateFileFinalizer.finalize(run_scope, before_manifest, sandbox) -> FinalizationResult`。
- Thread branch 使用 `PrivateFileService.copy_thread_files(source_scope, target_thread_id)`，不读取当前 sandbox。

- [ ] **Step 1: 写 restore hash、symlink、finalization-success ordering 失败测试**

```python
async def test_run_cannot_be_success_before_artifact_commit(worker):
    finalizer.fail_on_commit = True
    await worker.run(PRIVATE_RECORD)
    persisted = await runs.get(CONTEXT_A, PRIVATE_RECORD.run_id)
    assert persisted.status == "error"
    assert persisted.finalization_status == "error"

async def test_restore_removes_tampered_projection(projection, sandbox):
    repo.tamper_chunk(FILE_ID, 1)
    with pytest.raises(PrivateWorkUnavailable):
        await projection.restore(RUN_SCOPE, sandbox)
    assert not sandbox.exists("/mnt/user-data/uploads/report.pdf")
```

覆盖物理路径含 project/user/thread、只恢复 ready files、拒绝 symlink/越界/保留目录、cancel/interrupted 也执行合法 finalization、清理失败不改已提交终态、branch 同 scope copy 和跨 scope 404。

- [ ] **Step 2: 运行测试确认 middleware 仍扫描宿主目录**

Run: `cd backend && uv run pytest tests/test_private_sandbox_files.py tests/test_private_file_finalizer.py -q`

Expected: FAIL，projection/finalizer 不存在；UploadsMiddleware 仍通过 `sandbox_uploads_dir` 枚举。

- [ ] **Step 3: 实现 scope-aware sandbox acquisition 与 streaming restore**

临时根固定为：

```python
relative_root = PurePosixPath(
    "projects", str(project_id), "users", owner_user_id, "threads", thread_id
)
```

provider acquisition 接收 `PrivateResourceScope` opaque value；projection 按 logical path 排序、逐 chunk 写临时目标、fsync/close 后校验 whole hash，再原子 publish 给工具。任何失败删除本轮 partial projection。

- [ ] **Step 4: 改造 ThreadData/Uploads middleware 的 authority source**

项目 runtime 从 `RunContext.file_authority` 获取 manifest 与 visible uploaded files；legacy runtime 继续走现有 path helpers直到 cutover。middleware 不能从 runtime context 的 client dict构造 scope。

- [ ] **Step 5: 实现 workspace/output finalizer**

finalizer 在 authorization boundary 后：扫描 regular files → 规范化 path → 拒绝 symlink/越界/保留目录/limit → 对照 manifest → 按 path 稳定排序 streaming 写新 version → 为 presented output 写 artifact。所有交付文件提交后才把 `finalization_status='complete'` 并允许 RunManager 写 success/interrupted。

- [ ] **Step 6: 把 branch 改为数据库 authority copy**

同 project/owner branch 使用 `INSERT ... SELECT` 创建新 file metadata，再 streaming/SQL copy chunks；source 与 target Thread 都先 scoped lock。新 file/artifact 不复用旧 ID，不复制 deleted/staging 行。

- [ ] **Step 7: 跑 sandbox/worker/branch 回归并提交**

Run: `cd backend && uv run pytest tests/test_private_sandbox_files.py tests/test_private_file_finalizer.py tests/test_uploads_middleware_core_logic.py tests/test_thread_data_middleware.py tests/test_run_worker_rollback.py tests/test_threads_router.py -q`

```bash
git add backend/app/private_work backend/packages/harness/deerflow backend/tests
git commit -m "feat: project private files into run sandboxes"
```

---

### Task 9: 将 Memory 存储、队列和 prompt injection 改成 project-owner scope

**Files:**

- Create: `backend/packages/harness/deerflow/persistence/private_work/memory_repository.py`
- Create: `backend/app/private_work/memory_service.py`
- Modify: `backend/packages/harness/deerflow/agents/memory/storage.py`
- Modify: `backend/packages/harness/deerflow/agents/memory/updater.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py`
- Modify: `backend/app/gateway/routers/memory.py`
- Create: `backend/tests/test_private_memory_repository.py`
- Create: `backend/tests/test_private_memory_queue.py`
- Create: `backend/tests/test_private_memory_prompt.py`
- Modify: `backend/tests/test_memory_router.py`
- Modify: `backend/tests/test_memory_storage_user_isolation.py`
- Modify: `backend/tests/test_memory_queue_user_isolation.py`

**Interfaces:**

- `ProjectMemoryStorage` 实现现有 Memory storage protocol，但 key 是 `(project_id, owner_user_id, namespace)`。
- `MemoryQueueItem` 入队时冻结 project、owner、thread、run、namespace、membership version；timer 不从 ContextVar 回推。
- `PrivateMemoryService` 支持 status/list/reload/import/export/update/delete；Viewer 只能 read/export。

- [ ] **Step 1: 写 queue scope capture、cross-owner 和 secret filtering 失败测试**

```python
async def test_memory_queue_uses_enqueued_scope_after_context_changes(queue):
    item = queue.enqueue(
        context=CONTEXT_A, thread_id=THREAD_ID, run_id=RUN_ID,
        namespace="default", messages=VISIBLE_MESSAGES,
    )
    set_current_user(OTHER_USER)
    await queue.flush(item.key)
    assert await memories.facts(CONTEXT_A, "default") == EXPECTED_FACTS
    assert await memories.facts(CONTEXT_B, "default") == []
```

覆盖 hidden system context、tool secret、credential、其他 owner message 不进入 extractor；membership left 后不写不注入；rejoin 同 membership row 恢复；namespace 唯一；可选 pgvector 开/关行为。

- [ ] **Step 2: 运行测试确认 MemoryStorage 仍是 user file**

Run: `cd backend && uv run pytest tests/test_private_memory_repository.py tests/test_private_memory_queue.py tests/test_private_memory_prompt.py -q`

Expected: FAIL，PG memory repository 不存在；现有 storage key 只有 user/agent。

- [ ] **Step 3: 实现结构化 PostgreSQL Memory repository**

```python
stmt = select(UserProjectMemoryRow).where(
    UserProjectMemoryRow.project_id == context.project_id,
    UserProjectMemoryRow.owner_user_id == str(context.user_id),
    UserProjectMemoryRow.namespace == namespace,
)
```

fact parent/Thread/run 引用使用完整 scope；upsert 使用 expected version；默认 namespace 为 `default`，legacy per-agent namespace 为 `agent:{stable_agent_key}`，迁移时不猜合并。

- [ ] **Step 4: 改造 queue item 与 updater**

```python
@dataclass(frozen=True)
class MemoryQueueItem:
    scope: PrivateResourceScope
    thread_id: str
    run_id: str
    namespace: str
    membership_version: int
    messages: tuple[VisibleMemoryMessage, ...]
```

enqueue 前过滤 message visibility；flush 前 revalidate；storage/updater 不再读取 timer 执行时 ContextVar。legacy file storage 只供 migration 和 cutover 前 legacy API。

- [ ] **Step 5: 将 MemoryMiddleware/prompt injection 接到 project storage**

runtime 有 private scope 时只读取该 scope/namespace 并继续现有 token budget；无 private scope 且 marker 已 cutover 时 fail closed，不回退 global file。Memory API 的项目路由由 Task 11 挂载，legacy guard由 Task 12 完成。

- [ ] **Step 6: 跑 Memory 回归并提交**

Run: `cd backend && uv run pytest tests/test_private_memory_repository.py tests/test_private_memory_queue.py tests/test_private_memory_prompt.py tests/test_memory_router.py tests/test_memory_storage.py tests/test_memory_storage_user_isolation.py tests/test_memory_queue.py tests/test_memory_queue_user_isolation.py tests/test_memory_prompt_injection.py -q`

```bash
git add backend/packages/harness/deerflow/persistence/private_work backend/packages/harness/deerflow/agents/memory backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py backend/app/private_work backend/app/gateway/routers/memory.py backend/tests
git commit -m "feat: scope memory by project and owner"
```

---

### Task 10: 将 IM connection、OAuth state 和 inbound routing 改成项目作用域

**Files:**

- Modify: `backend/packages/harness/deerflow/persistence/channel_connections/sql.py`
- Create: `backend/app/private_work/connection_service.py`
- Create: `backend/app/private_work/connection_inbound.py`
- Modify: `backend/app/gateway/routers/channel_connections.py`
- Modify: `backend/app/channels/message_bus.py`
- Modify: `backend/app/channels/service.py`
- Modify: `backend/app/channels/feishu.py`
- Modify: `backend/app/channels/slack.py`
- Modify: `backend/app/channels/telegram.py`
- Modify: `backend/app/channels/discord.py`
- Modify: `backend/app/channels/dingtalk.py`
- Modify: `backend/app/projects/membership_service.py`
- Modify: `backend/app/projects/lifecycle_service.py`
- Create: `backend/tests/test_private_connection_repository.py`
- Create: `backend/tests/test_private_connection_service.py`
- Create: `backend/tests/test_private_connection_inbound.py`
- Modify: `backend/tests/test_channel_connections_repository.py`
- Modify: `backend/tests/test_channel_connections_router.py`
- Modify: `backend/tests/test_channels.py`

**Interfaces:**

- connection repository product methods全部要求 `PrivateResourceScope`；provider-global disconnect 迁入显式 trusted operations 方法。
- `ProjectConnectionService.begin_connect/complete_callback/list/disconnect` 每次 revalidate。
- `ConnectionInboundResolver.resolve(provider_identity) -> ResolvedInboundPrivateWork` 是内部 run 的唯一 project/owner来源。

- [ ] **Step 1: 写 connection identity、freeze/rejoin 与 inbound scope 失败测试**

```python
async def test_inbound_can_only_route_to_bound_project(resolver):
    resolved = await resolver.resolve(PROVIDER_IDENTITY)
    assert resolved.context.project_id == PROJECT_A
    assert resolved.context.user_id == OWNER_A
    assert resolved.thread_scope == OWNER_A_PROJECT_A_SCOPE

async def test_left_member_connection_is_frozen_and_secret_is_not_decrypted(service):
    await membership_service.leave(CONTEXT_A)
    with pytest.raises(PrivateWorkNotFound):
        await service.resolve_inbound(PROVIDER_IDENTITY)
    assert cipher.decrypt_calls == []
```

覆盖 project-owner unique、active external identity partial unique、OAuth callback membership change、owner header 不足以授权、conversation 完整 FK、同 identity 绑定新项目、rejoin collision 保持 frozen、不同 provider adapters统一路径。

- [ ] **Step 2: 运行测试确认 repository 只按 owner**

Run: `cd backend && uv run pytest tests/test_private_connection_repository.py tests/test_private_connection_service.py tests/test_private_connection_inbound.py -q`

Expected: FAIL，`upsert_connection` 不接受 project scope，inbound 仍依赖 owner header/context。

- [ ] **Step 3: 收紧 repository 与 OAuth state**

owner unique 改为 `(project_id, owner_user_id, provider, external_account_id, workspace_id)`；connected external identity partial unique排除 frozen/revoked。OAuth state 保存 project/owner/provider/expiry/safe redirect，consume 用 state hash lock 后重新解析 membership/capability。

- [ ] **Step 4: 实现连接 service 与冻结状态机**

create/rebind 要求 `private_work.create`；list/read 要求 read-own；disconnect 是 owner 删除权。leave/remove/suspend transaction 将 connection 置 `frozen`，保留 encrypted credential但禁止读取。rejoin 只有 external identity 未被其他 connected row占用才恢复，否则保持 frozen。

- [ ] **Step 5: 统一 inbound resolver 与项目 run path**

按 external identity → connection → project → membership → conversation → Thread 锁序解析；conversation 缺失时通过 `PrivateThreadService` 创建，随后只调用 Task 5 `start_private_run`。删掉/拒绝从 `X-DeerFlow-Owner-User-Id` 或 message payload直接构造 project context 的路径。

- [ ] **Step 6: 跑 channel/provider 回归并提交 Gate 3**

Run: `cd backend && uv run pytest tests/test_private_connection_repository.py tests/test_private_connection_service.py tests/test_private_connection_inbound.py tests/test_channel_connections_repository.py tests/test_channel_connections_router.py tests/test_channels.py tests/test_feishu_parser.py tests/test_slack_channel_connections.py tests/test_telegram_channel_connections.py tests/test_discord_channel_connections.py tests/test_dingtalk_channel.py -q`

```bash
git add backend/packages/harness/deerflow/persistence/channel_connections backend/app/private_work backend/app/gateway/routers/channel_connections.py backend/app/channels backend/app/projects backend/tests
git commit -m "feat: scope IM connections to private projects"
```

---

### Task 11: 挂载 project private-work、Memory 和 connection API

**Files:**

- Create: `backend/app/gateway/routers/private_work.py`
- Create: `backend/app/gateway/routers/project_memory.py`
- Create: `backend/app/gateway/routers/project_connections.py`
- Create: `backend/app/gateway/private_work_schemas.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/deps.py`
- Refactor: `backend/app/gateway/routers/threads.py`
- Refactor: `backend/app/gateway/routers/thread_runs.py`
- Refactor: `backend/app/gateway/routers/runs.py`
- Refactor: `backend/app/gateway/routers/feedback.py`
- Refactor: `backend/app/gateway/routers/uploads.py`
- Refactor: `backend/app/gateway/routers/artifacts.py`
- Create: `backend/tests/test_private_work_router.py`
- Create: `backend/tests/test_project_memory_router.py`
- Create: `backend/tests/test_project_connections_router.py`
- Create: `backend/tests/test_private_work_route_dependencies.py`

**Interfaces:**

- Base: `/api/projects/{project_id}/private-work`。
- Memory: `/api/projects/{project_id}/memory`；connections: `/api/projects/{project_id}/connections`。
- `GET /api/projects/{project_id}/private-work/readiness` 只返回 `ready|migration_required|unavailable`、公共错误code和request ID，不返回inventory或私有counts。
- 项目 router 只解析 UUID、strict Pydantic schema、`PrivateWorkContext` 和 error mapping；复用 service/serialization helpers，不复制业务逻辑。

- [ ] **Step 1: 写 route matrix、field stripping 和 anti-enumeration 失败测试**

至少覆盖：

```python
PROJECT_PRIVATE_ROUTES = (
    "POST /threads", "POST /threads/search", "GET /threads/{thread_id}",
    "PATCH /threads/{thread_id}", "DELETE /threads/{thread_id}",
    "GET /threads/{thread_id}/state", "POST /threads/{thread_id}/history",
    "POST /threads/{thread_id}/state", "GET|PUT|DELETE /threads/{thread_id}/goal",
    "POST /threads/{thread_id}/compact", "POST /threads/{thread_id}/branch",
    "POST /threads/{thread_id}/runs", "POST /threads/{thread_id}/runs/stream",
    "POST /threads/{thread_id}/runs/wait", "GET /threads/{thread_id}/runs",
    "GET|DELETE /threads/{thread_id}/runs/{run_id}",
    "GET /threads/{thread_id}/runs/{run_id}/join",
    "GET /threads/{thread_id}/messages", "GET /threads/{thread_id}/events",
    "GET /threads/{thread_id}/token-usage", "POST /threads/{thread_id}/runs/{run_id}/feedback",
    "POST|GET|DELETE /threads/{thread_id}/uploads", "GET /artifacts/{artifact_id}",
)
```

每类测试 same-project other-owner 和 cross-project 都是 404；同 scope capability 缺失为 403；invalid UUID/path/body 422；unavailable dependency 503；client authority fields 被 drop。

readiness测试证明complete marker前返回 `migration_required`、marker后返回 `ready`，数据库不可用返回 `unavailable`；响应不得包含owner、Thread、file、Memory或connection count。

- [ ] **Step 2: 运行测试确认路由未挂载**

Run: `cd backend && uv run pytest tests/test_private_work_router.py tests/test_project_memory_router.py tests/test_project_connections_router.py tests/test_private_work_route_dependencies.py -q`

Expected: FAIL，project private routes 返回 404。

- [ ] **Step 3: 定义唯一 context dependency 和 strict route class**

```python
async def private_work_context(
    project_id: uuid.UUID,
    user=Depends(get_current_user_from_request),
    session=Depends(project_session),
) -> PrivateWorkContext:
    project = await resolve_project_context(
        session, uuid.UUID(str(user.id)), project_id, request_id()
    )
    return PrivateWorkContext.from_project(project)
```

`PrivateWorkRoute` 将 FastAPI validation error 映射为 `PRIVATE_WORK_INVALID`。Pydantic `extra='forbid'`；自由格式 LangGraph config/context 在进入 service 前调用 authoritative field stripper。

- [ ] **Step 4: 抽取 legacy serializer/helpers，项目 router 调 service**

从现有 routers 抽取 response serialization、SSE/wait consumer、input normalization等无授权逻辑 helper；项目 router 不调用 legacy `@require_permission`、不调用 raw `get_checkpointer`、不读取 `get_effective_user_id()`。

- [ ] **Step 5: 挂载 Memory/connection 项目 API**

Memory list/status/reload/import/export/update/delete 全部使用 Task 9 service；secret-bearing connection create/replace 使用 imperative request schema和 `Cache-Control: no-store`，response 永不回显 token/credential。

- [ ] **Step 6: app/deps 安装 scoped repositories/services**

`langgraph_runtime` 从同一个 session factory初始化 private repositories、M3 resolver、scoped checkpointer、file/memory/connection services；FastAPI lifespan teardown顺序仍先 drain runs，再关闭 checkpointer/engine。

- [ ] **Step 7: 跑 API 与现有兼容路由回归并提交**

Run: `cd backend && uv run pytest tests/test_private_work_router.py tests/test_project_memory_router.py tests/test_project_connections_router.py tests/test_private_work_route_dependencies.py tests/test_threads_router.py tests/test_runs_api_endpoints.py tests/test_memory_router.py tests/test_channel_connections_router.py tests/test_uploads_router.py tests/test_artifacts_router.py -q`

```bash
git add backend/app/gateway backend/app/private_work backend/tests
git commit -m "feat: expose project private work APIs"
```

---

### Task 12: 在 cutover marker 后关闭所有 legacy private runtime 入口

**Files:**

- Create: `backend/app/private_work/cutover.py`
- Modify: `backend/app/gateway/routers/threads.py`
- Modify: `backend/app/gateway/routers/thread_runs.py`
- Modify: `backend/app/gateway/routers/runs.py`
- Modify: `backend/app/gateway/routers/memory.py`
- Modify: `backend/app/gateway/routers/channel_connections.py`
- Modify: `backend/app/gateway/routers/uploads.py`
- Modify: `backend/app/gateway/routers/artifacts.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/packages/harness/deerflow/client.py`
- Create: `backend/tests/test_private_work_cutover_guard.py`
- Modify: `backend/tests/test_tui_runtime.py`
- Modify: `backend/tests/test_stateless_runs_owner_isolation.py`

**Interfaces:**

- `PrivateWorkCutoverGuard.require_legacy_open()` 读取 singleton state；complete 后抛 `PrivateWorkCutover`。
- `require_project_open()` 仅在 final schema + `cutover_complete` marker 后允许项目 API；未完成为 409。
- embedded/TUI client cutover 后必须显式注入可信 private scope adapter，client dict 不被接受。

- [ ] **Step 1: 写 marker 前后 route/runtime matrix 失败测试**

```python
@pytest.mark.parametrize("path", [
    "/api/threads", "/api/runs", "/api/memory", "/api/channels/connections",
])
async def test_legacy_private_api_returns_cutover_conflict(client, cutover_complete, path):
    response = await client.get(path)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_CUTOVER"
```

同时证明 legacy API 在 marker 后不能 search/get 猜到任何 project row；项目 API 在 marker 前也不能创建；stateless run、TUI、embedded client、IM owner-header bypass 全部 fail closed。

- [ ] **Step 2: 运行测试确认 marker 尚未影响 legacy routes**

Run: `cd backend && uv run pytest tests/test_private_work_cutover_guard.py tests/test_stateless_runs_owner_isolation.py tests/test_tui_runtime.py -q`

Expected: FAIL，legacy routes 仍返回数据或项目 route 在 marker 前可进入。

- [ ] **Step 3: 实现 request/runtime 两层 guard**

所有 legacy private routers 在 auth 后、任何 repository/checkpointer/file访问前调用 guard；`start_run`、embedded client的 checkpoint/tool loading也调用 runtime guard，防止绕过 HTTP。项目 router 先要求 final schema与 complete marker。

Gateway startup 的 `_migrate_orphaned_threads` 在 complete marker 后禁用；scope backfill只允许Task 13显式migration执行，普通启动不得再做unscoped owner修复。

- [ ] **Step 4: 禁止 default-project 推断和 legacy/project 双写**

删除任何“唯一项目”“最近项目”“default slug”私有 scope fallback。marker 后 project service 是唯一写路径，legacy filesystem/memory sources只读保留到 M7；不得同时写 PG authority 与 legacy private source。

- [ ] **Step 5: 跑 cutover/legacy 回归并提交**

Run: `cd backend && uv run pytest tests/test_private_work_cutover_guard.py tests/test_stateless_runs_owner_isolation.py tests/test_tui_runtime.py tests/test_threads_router.py tests/test_runs_api_endpoints.py tests/test_memory_router.py tests/test_channel_connections_router.py -q`

```bash
git add backend/app/private_work/cutover.py backend/app/gateway/routers backend/packages/harness/deerflow/client.py backend/tests
git commit -m "feat: close legacy private APIs after M4 cutover"
```

---

### Task 13: 实现 staged private-work migration、backup proof、ledger 与 cutover

**Files:**

- Create: `backend/scripts/migrate_private_work.py`
- Modify: `backend/Makefile`
- Modify: `Makefile`
- Create: `backend/tests/test_private_work_migration.py`
- Create: `backend/tests/test_private_work_migration_cli.py`
- Create: `backend/tests/integration/test_m4_private_work_migration_postgres.py`
- Modify: `backend/tests/test_migrate_assets.py`

**Interfaces:**

- CLI: `--dry-run | --execute`、`--owner-map`、`--backup-dir`、可选 `--repo-root/--data-root`；execute 从 `DEER_FLOW_M4_BACKUP_KEY` 读取 32-byte key，不写数据库或仓库。
- owner map 精确映射 legacy owner user ID → active project UUID；禁止 email/role/recent/default/unique-project 推断。
- domain 顺序固定：thread/run/event/feedback → checkpoint marker → files/artifacts → Memory → connections → probes → finalize → cutover marker。

- [ ] **Step 1: 写 parser、inventory、dry-run 零写入和 redaction 失败测试**

```python
def test_owner_map_requires_explicit_active_project_for_every_legacy_owner(tmp_path):
    owner_map = write_owner_map(tmp_path, {str(OWNER_A): str(PROJECT_A)})
    inventory = inventory_with_owners(OWNER_A, OWNER_B)
    with pytest.raises(PrivateWorkMigrationError, match="owner map incomplete"):
        build_migration_plan(inventory, owner_map)

async def test_dry_run_does_not_mutate_target_or_write_backup(migration):
    before = await target_fingerprint()
    report = await migration.run(dry_run=True)
    assert await target_fingerprint() == before
    assert not report.backup_written
```

输出测试扫描不得出现 user ID/email、prompt、message、Memory、文件名/path/content、credential、数据库 URL；只允许 counts、stable key hash、size、target scope hash 和公共 conflict code。

- [ ] **Step 2: 写真实 PostgreSQL staged/finalize/幂等/tamper 失败测试**

覆盖：

- `0007 -> 0008` expand；
- source fingerprints 在 expand 前、回填前、finalize 前三次一致；
- owner map 成员必须 active；
- DB backup proof 缺失/过期/指纹不符 fail closed；
- filesystem backup 为 authenticated encryption，目录 0700、文件 0600；
- checkpoint 只改 metadata marker，不改 checkpoint/blob payload bytes；
- 每 domain ledger 同事务提交，中断后 same source/target no-op；
- source fingerprint change 或 target tamper fail closed；
- cross-project/cross-owner probe 通过后才能写 finalize prerequisite；
- `0009` 后才写 complete marker；legacy source bytes 保持不变。

- [ ] **Step 3: 运行测试确认脚本/target 缺失**

Run: `cd backend && uv run pytest tests/test_private_work_migration.py tests/test_private_work_migration_cli.py tests/integration/test_m4_private_work_migration_postgres.py -q`

Expected: FAIL，CLI 模块或 migration tables/control flow 不存在；真实 PG fixture 不能以 skip 代替红灯。

- [ ] **Step 4: 定义 immutable inventory 与 plan types**

```python
@dataclass(frozen=True)
class PrivateWorkInventory:
    source_fingerprint: str
    owners: tuple[LegacyOwnerInventory, ...]
    threads: tuple[ThreadInventoryItem, ...]
    checkpoints: tuple[CheckpointInventoryItem, ...]
    files: tuple[FileInventoryItem, ...]
    memories: tuple[MemoryInventoryItem, ...]
    connections: tuple[ConnectionInventoryItem, ...]

@dataclass(frozen=True)
class OwnerTarget:
    owner_user_id: str = field(repr=False)
    project_id: uuid.UUID
    membership_id: uuid.UUID
```

inventory 使用 pinned no-follow file descriptors 和 canonical database ordering；所有 raw private payload `repr=False`。plan 在任何 target write前完整验证 scope graph、重复 logical paths、checkpoint coverage 和 connection routing。

- [ ] **Step 5: 实现 backup proof 与认证加密 filesystem backup**

execute 要求 operator 放入 database backup proof manifest，包含受控 backup locator 的安全相对值、DB fingerprint、timestamp 和 SHA-256；脚本只验证，不生成通用 DB backup。filesystem archive 使用 AES-GCM、随机 nonce、AAD 绑定 migration run/source fingerprint；key 只从环境读，backup manifest 不保存 key/nonce以外的解密材料。

- [ ] **Step 6: 实现幂等 domain executor 与 checkpoint marker migration**

每个 domain transaction：lock migration run → recheck source/target digest → write rows/chunks/markers → probes/count/hash → ledger commit。checkpoint 使用上游 saver/SQL metadata encoding兼容路径，只覆盖 server scope marker，checkpoint/blob/write payload hash 在前后相同。

若 inventory 证明数据库与 filesystem 均无 legacy private source，则执行 empty-install路径：验证final schema、运行空域cross-scope probe并写 `empty_install` complete marker，不创建伪owner/Thread/Memory数据。

- [ ] **Step 7: 实现 finalize 与 marker 顺序**

全部 domain/semantic/probe ledger complete 后写 `migration_ready` prerequisite，运行 Alembic `upgrade 0009_project_private_work_finalize`，验证 revision/constraints，再以独立短事务写 `cutover_complete`。任意失败不写 marker，项目 API保持 409。

- [ ] **Step 8: 增加 Make targets**

```make
migrate-private-work:
	@uv run python scripts/migrate_private_work.py $(ARGS)
```

Root `Makefile` 转发同名 target，并在 `help` 显示 dry-run/execute 说明；`.PHONY` 同步。

- [ ] **Step 9: 跑 migration 全套并提交**

Run: `cd backend && uv run pytest tests/test_private_work_migration.py tests/test_private_work_migration_cli.py tests/integration/test_m4_private_work_migration_postgres.py tests/test_migrate_assets.py -q`

```bash
git add backend/scripts/migrate_private_work.py backend/Makefile Makefile backend/tests
git commit -m "feat: migrate legacy private work into projects"
```

---

### Task 14: 建立 account/project-scoped LangGraph client 与 cancel-before-clear cache ownership

**Files:**

- Create: `frontend/src/core/private-work/types.ts`
- Create: `frontend/src/core/private-work/api-client.ts`
- Create: `frontend/src/core/private-work/query-keys.ts`
- Create: `frontend/src/core/private-work/scope-registry.ts`
- Create: `frontend/src/core/private-work/provider.tsx`
- Modify: `frontend/src/core/config/index.ts`
- Modify: `frontend/src/core/api/api-client.ts`
- Modify: `frontend/src/core/threads/hooks.ts`
- Modify: `frontend/src/core/threads/api.ts`
- Modify: `frontend/src/core/uploads/api.ts`
- Modify: `frontend/src/core/uploads/hooks.ts`
- Modify: `frontend/src/components/projects/project-context.tsx`
- Create: `frontend/tests/unit/core/private-work/api-client.test.ts`
- Create: `frontend/tests/unit/core/private-work/query-keys.test.ts`
- Create: `frontend/tests/unit/core/private-work/scope-registry.test.ts`
- Create: `frontend/tests/unit/core/private-work/provider.test.tsx`
- Modify: `frontend/tests/unit/core/api/api-client.test.ts`
- Modify: `frontend/tests/unit/core/projects/account-query-client.test.ts`

**Interfaces:**

- `getProjectAPIClient({ accountId, projectId })` cache key包含两者，base URL为 `/api/projects/{project_id}/private-work`。
- `ProjectPrivateWorkProvider` 是 nested project pages 的 client/query scope owner；只消费 `useCurrentProject()` 与 authenticated account ID。
- 所有 private keys 以 `['account', accountId, 'project', projectId, 'private-work', ...]` 开始。

- [ ] **Step 1: 写 base URL、client identity 和 cancel-before-clear 失败测试**

```ts
it("does not share a LangGraph client across accounts or projects", () => {
  const a1 = getProjectAPIClient({ accountId: A, projectId: P1 });
  const a2 = getProjectAPIClient({ accountId: A, projectId: P2 });
  const b1 = getProjectAPIClient({ accountId: B, projectId: P1 });
  expect(a1).not.toBe(a2);
  expect(a1).not.toBe(b1);
  expect(apiUrlOf(a1)).toEndWith(`/api/projects/${P1}/private-work`);
});

it("cancels in-flight work before removing scope state", async () => {
  const transition = transitionPrivateWorkScope(registry, queryClient, A_P1, A_P2);
  expect(queryClient.cancelQueries).toHaveBeenCalledBefore(registry.dispose);
  await transition;
  expect(registry.has(A_P1)).toBe(false);
});
```

覆盖 logout、account switch、project switch、provider unmount、late query/mutation response、static mode、CSRF injection、client secret不进入cache。

- [ ] **Step 2: 运行单测确认只有 module-level default client**

Run: `cd frontend && pnpm test -- --run tests/unit/core/private-work/api-client.test.ts tests/unit/core/private-work/query-keys.test.ts tests/unit/core/private-work/scope-registry.test.ts tests/unit/core/private-work/provider.test.tsx`

Expected: FAIL，新模块不存在；现有 `getAPIClient()` 只以 `default|mock` cache。

- [ ] **Step 3: 定义 project base URL 和独立 client registry**

```ts
export function projectPrivateWorkBaseURL(projectId: string): string {
  return `${getBackendBaseURL()}/api/projects/${projectIdSchema.parse(projectId)}/private-work`;
}

const clients = new Map<string, LangGraphClient>();
export function getProjectAPIClient(scope: ProjectClientScope) {
  const parsed = projectClientScopeSchema.parse(scope);
  const key = `${parsed.accountId}:${parsed.projectId}`;
  return getOrCreate(clients, key, () => createCompatibleClient({
    apiUrl: projectPrivateWorkBaseURL(parsed.projectId),
  }));
}
```

复用现有 CSRF、run stream sanitize、terminal reconnect/cancel wrappers，但 registry 与 default client完全分开。

- [ ] **Step 4: 让 threads/uploads hooks 从 provider 获取 client/scope**

将 hooks 的低层 query functions改为显式 `client`/`scope` 参数；workspace provider传 default client，project provider传 project client。禁止在 hook body重新调用 module global `getAPIClient()`。query keys 和 sessionStorage reconnect key加入 account/project，避免相同 thread UUID 状态串线。

- [ ] **Step 5: 实现 provider transition lifecycle**

scope变化时先 abort controllers、`queryClient.cancelQueries({queryKey: privateRoot(old)})`，再 remove old queries/mutations/reconnect state/client。late response使用 generation token丢弃；注销仍由 account transition清全局 QueryClient，但 project registry也必须 dispose。

- [ ] **Step 6: 跑 frontend core 回归并提交**

Run: `cd frontend && pnpm test -- --run tests/unit/core/private-work tests/unit/core/api/api-client.test.ts tests/unit/core/projects/account-query-client.test.ts tests/unit/core/threads tests/unit/core/uploads`

```bash
git add frontend/src/core frontend/src/components/projects/project-context.tsx frontend/tests/unit/core
git commit -m "feat: isolate project private work clients"
```

---

### Task 15: 复用现有 chat 体验建立项目 chats、最近工作和 capability 门禁

**Files:**

- Create: `frontend/src/app/projects/[project_slug]/chats/page.tsx`
- Create: `frontend/src/app/projects/[project_slug]/chats/[thread_id]/page.tsx`
- Create: `frontend/src/app/projects/[project_slug]/chats/[thread_id]/layout.tsx`
- Create: `frontend/src/components/projects/private-work/project-chats-page.tsx`
- Create: `frontend/src/components/projects/private-work/project-chat-page.tsx`
- Create: `frontend/src/components/projects/private-work/project-chat-providers.tsx`
- Create: `frontend/src/components/projects/private-work/agent-selector-dialog.tsx`
- Create: `frontend/src/components/projects/private-work/recent-private-work.tsx`
- Modify: `frontend/src/app/workspace/chats/[thread_id]/page.tsx`
- Modify: `frontend/src/components/workspace/chats/use-thread-chat.ts`
- Modify: `frontend/src/components/workspace/chats/use-chat-mode.ts`
- Modify: `frontend/src/components/projects/project-private-work-cta.tsx`
- Modify: `frontend/src/components/projects/project-home.tsx`
- Modify: `frontend/src/components/projects/project-nav.tsx`
- Create: `frontend/tests/unit/components/projects/private-work/project-chat.test.tsx`
- Create: `frontend/tests/unit/components/projects/private-work/agent-selector.test.tsx`
- Create: `frontend/tests/unit/components/projects/private-work/recent-private-work.test.tsx`
- Modify: `frontend/tests/unit/components/projects/project-components.test.ts`
- Create: `frontend/tests/e2e/project-private-chat.spec.ts`

**Interfaces:**

- workspace/project chat 共享一个 scope-aware chat implementation；不复制 1000+ 行 workspace page。
- Project chat route只调用 `useCurrentProject()`，不重复 slug resolution/enter mutation。
- new Thread 必须先选择 visible executable Agent；Thread 保存 logical Agent，run admission重新解析 exact version。

- [ ] **Step 1: 写 route、Agent selector、Viewer 和 404 失败测试**

```tsx
it("viewer never dispatches create or run requests", async () => {
  render(<ProjectPrivateWorkCta project={viewerProject} />);
  await user.click(screen.getByRole("button", { name: "开始私有对话" }));
  expect(createThread).not.toHaveBeenCalled();
  expect(screen.getByText("你可以查看自己的既有对话，但不能创建新工作")).toBeVisible();
});
```

覆盖有能力+有 Agent打开 selector；无 Agent跳 `/agents`；recent只显示当前 owner；static demo无入口；same-project other owner和cross-project direct URL都渲染统一 not-found；项目 page不调用 project list API。

- [ ] **Step 2: 运行单测确认 project chat routes/组件缺失**

Run: `cd frontend && pnpm test -- --run tests/unit/components/projects/private-work tests/unit/components/projects/project-components.test.ts`

Expected: FAIL，routes/components 不存在，CTA仍 disabled。

- [ ] **Step 3: 抽取 scope-aware chat page**

从 workspace ChatPage 提取共享 `ScopedChatPage`，输入：

```ts
export interface ChatRouteScope {
  client: LangGraphClient;
  threadBasePath: string;
  newThreadPath: string;
  canCreate: boolean;
  canRun: boolean;
  canUpload: boolean;
  canDelete: boolean;
  scheduledTasksVisible: boolean;
}
```

workspace adapter保持现有行为；project adapter使用 project client/base path，隐藏 scheduled-task link。stream、stop、goal、compact、branch、human-input、sidecar、artifact、token usage都继续走共享组件。

- [ ] **Step 4: 实现项目 chats list/detail/providers**

list使用 project-scoped infinite query；links固定 `/projects/${slug}/chats/${thread_id}`。detail providers从 `useCurrentProject()` 和 auth account构造 Task 14 provider；404只显示公共 not-found，不显示 owner/project差异。

- [ ] **Step 5: 实现 Agent selector、CTA 与 recent work**

selector消费 M3 project asset catalog的可执行 Agent视图；创建 Thread request只发送 `agent_asset_id/agent_scope`，不发送 version/owner/capability。项目首页 recent query limit固定，创建成功导航 chats/thread。无 executable Agent跳项目 Agents；Viewer只读提示。

- [ ] **Step 6: 项目导航加入 chats，保持 feature flag关闭**

在 Task 17 前仍由 cutover/readiness状态决定 CTA disabled；`PROJECT_PRIVATE_WORKSPACE` 暂不改 true。project nav可以在 readiness为complete时显示 Chats，Memory/connections在 Task 16接入。

- [ ] **Step 7: 跑 unit/E2E 项目聊天回归并提交**

Run: `cd frontend && pnpm test -- --run tests/unit/components/projects/private-work tests/unit/core/private-work tests/unit/core/threads`

Run: `cd frontend && pnpm exec playwright test tests/e2e/project-private-chat.spec.ts`

```bash
git add frontend/src/app/projects frontend/src/app/workspace/chats frontend/src/components/projects frontend/src/components/workspace/chats frontend/tests
git commit -m "feat: add project private chat experience"
```

---

### Task 16: 建立项目 Memory、connections 和 file sidecar 页面

**Files:**

- Create: `frontend/src/app/projects/[project_slug]/memory/page.tsx`
- Create: `frontend/src/app/projects/[project_slug]/connections/page.tsx`
- Create: `frontend/src/core/private-work/memory.ts`
- Create: `frontend/src/core/private-work/connections.ts`
- Create: `frontend/src/core/private-work/files.ts`
- Create: `frontend/src/components/projects/private-work/project-memory-page.tsx`
- Create: `frontend/src/components/projects/private-work/project-connections-page.tsx`
- Modify: `frontend/src/components/workspace/settings/memory-settings-page.tsx`
- Modify: `frontend/src/components/workspace/channels/workspace-channels-list.tsx`
- Modify: `frontend/src/components/workspace/sidecar/sidecar-panel.tsx`
- Modify: `frontend/src/components/workspace/artifacts/artifact-file-detail.tsx`
- Modify: `frontend/src/components/projects/project-nav.tsx`
- Create: `frontend/tests/unit/core/private-work/memory.test.ts`
- Create: `frontend/tests/unit/core/private-work/connections.test.ts`
- Create: `frontend/tests/unit/core/private-work/files.test.ts`
- Create: `frontend/tests/unit/components/projects/private-work/project-memory.test.tsx`
- Create: `frontend/tests/unit/components/projects/private-work/project-connections.test.tsx`
- Create: `frontend/tests/e2e/project-private-data.spec.ts`

**Interfaces:**

- Memory/connection/file requests都从 current project ID构造；query key含 account/project。
- connection secret-bearing create/replace使用 local component state + imperative API，不进入 TanStack mutation variables/cache/devtools。
- files仍只在 chat sidecar展示，不新增独立文件管理页。

- [ ] **Step 1: 写 project paths、Viewer mutation deny 和 secret-cache 失败测试**

```ts
it("never stores a connection secret in query or mutation cache", async () => {
  await submitConnectionSecret(scope, { provider: "slack", token: SECRET });
  expect(JSON.stringify(queryClient.getQueryCache().getAll())).not.toContain(SECRET);
  expect(JSON.stringify(queryClient.getMutationCache().getAll())).not.toContain(SECRET);
});
```

覆盖 Memory list/export/delete/update、Viewer read/export/delete-own与modify deny、connection list/connect/disconnect/rebind、upload/list/delete/artifact project URLs、cross scope 404、download streaming UI、logout/project switch late response。

- [ ] **Step 2: 运行单测确认现有 API 都是 global path**

Run: `cd frontend && pnpm test -- --run tests/unit/core/private-work/memory.test.ts tests/unit/core/private-work/connections.test.ts tests/unit/core/private-work/files.test.ts tests/unit/components/projects/private-work/project-memory.test.tsx tests/unit/components/projects/private-work/project-connections.test.tsx`

Expected: FAIL，新 API/components不存在；现有 memory/channel/upload modules使用 global路径。

- [ ] **Step 3: 抽取可注入 base URL 的共享 view models**

workspace legacy adapters保留到 M7；project adapters传 `/api/projects/{id}/memory|connections` 和 private-work file URLs。UI共享纯 view component，authorization decisions只使用服务端 capabilities。

- [ ] **Step 4: 实现 Memory 页面**

Runner/Admin/Editor可 reload/import/update/delete/export；Viewer只有 list/export/delete-own，修改控件不渲染。import input做 strict validation；响应 schema用 Zod strict parse；错误使用 Task 2 public code文案。

- [ ] **Step 5: 实现 connections 页面和 imperative secret submit**

provider discovery/list可 query；connect/secret submit由 `fetchWithAuth` 直接发出，body只存在调用栈和受控 local state，finally清空 state。OAuth redirect只接受服务端 safe redirect metadata。

- [ ] **Step 6: 接入 project file/artifact sidecar**

sidecar从 ChatRouteScope获得 file/artifact API；project scope下禁止 fallback到 `/api/threads` 或宿主 path。Viewer可以下载/删除 own file，但 upload按钮隐藏；run者可上传。

- [ ] **Step 7: 跑 unit/E2E 并提交**

Run: `cd frontend && pnpm test -- --run tests/unit/core/private-work tests/unit/components/projects/private-work tests/unit/core/channels tests/unit/core/uploads`

Run: `cd frontend && pnpm exec playwright test tests/e2e/project-private-data.spec.ts`

```bash
git add frontend/src/app/projects frontend/src/core/private-work frontend/src/components/projects frontend/src/components/workspace frontend/tests
git commit -m "feat: add project memory connections and files"
```

---

### Task 17: 建立 M4 真实 PostgreSQL/Frontend release gate 并开放入口

**Files:**

- Create: `backend/tests/integration/test_m4_private_work_postgres.py`
- Create: `backend/tests/support/m4_private_work.py`
- Modify: `.github/workflows/project-foundation-postgres-tests.yml`
- Modify: `frontend/src/core/projects/features.ts`
- Modify: `frontend/src/components/projects/project-private-work-cta.tsx`
- Modify: `frontend/tests/e2e/projects.spec.ts`
- Create: `frontend/tests/e2e/project-private-work-isolation.spec.ts`

**Interfaces:**

- 一份真实 PG M4 gate覆盖 owner A/project A、member B/project A、owner A/project B、outsider、四角色、system/project Agent和含 credential grant MCP。
- CI固定运行 M1 cutover、M1 isolation、M2 governance、M3 shared assets、M4 private work和M4 migration。
- feature入口只有后端 readiness/cutover complete且前端编译期开关开启时可用。

- [ ] **Step 1: 写完整真实 PostgreSQL integration gate**

在单一随机测试库覆盖规格 17.1 全部矩阵：Thread/run/event/feedback/file/artifact/Memory/connection CRUD/search/page/export/guesstimate UUID；同项目跨 owner 404；跨项目 404；Viewer read-own/create deny；复合 FK；checkpoint marker缺失/伪造/cross scope；exact snapshot/generation stale；secret零持久化；authorization revocation；connection inbound/freeze/rejoin；file chunk/hash/tamper/finalization。

secret zero assertion 扫描：

```python
for table in PRIVATE_PERSISTENCE_TABLES + LANGGRAPH_CHECKPOINT_TABLES:
    assert SECRET_BYTES not in await dump_table_bytes(connection, table)
assert SECRET_TEXT not in caplog.text
```

- [ ] **Step 2: 运行 M4 gate确认任何漏项失败**

Run: `cd backend && uv run pytest tests/integration/test_m4_private_work_postgres.py tests/integration/test_m4_private_work_migration_postgres.py -q`

Expected: 首次运行若有任一未实现隔离/secret/file/cutover行为则 FAIL；缺 `POSTGRES_TEST_URL` 时必须先配置并重跑。

- [ ] **Step 3: 扩展 CI PostgreSQL workflow**

workflow name/job/step改为 `M1, M2, M3 and M4 PostgreSQL Gates`，pytest列表固定包含：

```text
tests/integration/test_m1_postgres_cutover.py
tests/integration/test_project_isolation_postgres.py
tests/integration/test_m2_project_governance_postgres.py
tests/integration/test_m3_shared_assets_postgres.py
tests/integration/test_m4_private_work_postgres.py
tests/integration/test_m4_private_work_migration_postgres.py
```

保留 least-privilege application role与 `POSTGRES_TEST_URL` hard fail；timeout按实测只做必要上调。

- [ ] **Step 4: 写 Frontend isolation E2E**

Playwright覆盖 account/project switch cancel-before-clear、recent owner isolation、Viewer、direct URL 404、Agent selector/run stream/stop/goal/compact/branch/human-input、upload/artifact、Memory、connection和static demo无入口。

- [ ] **Step 5: fresh install/cutover readiness 通过后开放入口**

把 `PROJECT_PRIVATE_WORKSPACE` 改为 `true as const`，但 CTA仍读取后端 readiness；marker不完整时 disabled且不导航。删除“后续里程碑”文案，改为准确的 capability/readiness状态。

- [ ] **Step 6: 跑 M1–M4 PG gate 与 frontend E2E并提交**

Run: `cd backend && uv run pytest tests/integration/test_m1_postgres_cutover.py tests/integration/test_project_isolation_postgres.py tests/integration/test_m2_project_governance_postgres.py tests/integration/test_m3_shared_assets_postgres.py tests/integration/test_m4_private_work_postgres.py tests/integration/test_m4_private_work_migration_postgres.py -q`

Run: `cd frontend && pnpm exec playwright test tests/e2e/projects.spec.ts tests/e2e/project-private-chat.spec.ts tests/e2e/project-private-data.spec.ts tests/e2e/project-private-work-isolation.spec.ts`

```bash
git add backend/tests/integration backend/tests/support .github/workflows/project-foundation-postgres-tests.yml frontend/src/core/projects/features.ts frontend/src/components/projects/project-private-work-cta.tsx frontend/tests/e2e
git commit -m "test: enforce M4 private work release gates"
```

---

### Task 18: 同步文档、执行全量验证并完成独立审查

**Files:**

- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `backend/AGENTS.md`
- Modify: `frontend/AGENTS.md`
- Modify: `docs/superpowers/specs/2026-07-12-project-first-saas-design.md`
- Modify: `docs/superpowers/specs/2026-07-14-project-private-work-m4-design.md`
- Create: `docs/operations/m4-private-work-migration.md`
- Modify: `docs/superpowers/plans/2026-07-14-project-private-work-m4.md`

**Interfaces:**

- 只有所有 fresh verification 有成功输出且独立 review无 Critical/Important finding，才将 M4 状态改为已完成、计划 checkbox全部勾选、总进度改为 4/8（50%）。
- 文档继续声明 M5 automation、M6 Worker/SSE/配额审计备份恢复、M7 legacy cleanup和M8完整发布验收未完成，系统仍不可作为完整多用户 SaaS发布。

- [ ] **Step 1: 先写文档一致性失败测试/检查**

使用 `rg` 证明当前文档仍含 `3/8`、`M4 未开始`、`私有工作后续里程碑`、`M1/M2/M3 gate`，记录需要替换的精确位置；不要在实现门禁未通过前改状态。

- [ ] **Step 2: 更新用户与架构文档**

README 中写项目 chats/files/Memory/connections、Viewer行为、legacy cutover、migration命令。Root/backend/frontend AGENTS分别记录 M4 authority、scope/checkpointer/file/memory/connection、project client/cache ownership和 M1–M4 release gate。

- [ ] **Step 3: 写运维 runbook**

runbook 必须包含：

1. dry-run 可在线执行；
2. maintenance window停止 Gateway、Scheduler、channel workers、embedded/TUI writers；
3. operator DB backup proof；
4. owner map格式与active membership检查；
5. `DEER_FLOW_M4_BACKUP_KEY`安全注入；
6. dry-run → execute → `make check-db` → M4 probes；
7. `0009` 前失败的恢复/幂等重跑；
8. `0009` 后 marker 前失败的决策；
9. cutover 后不得启动 legacy writer；
10. M4 不承诺通用 backup/restore或物理 retention purge。

- [ ] **Step 4: 运行 backend focused + lint/format/blocking-I/O**

Run:

```bash
cd backend
uv run pytest tests/test_private_work_*.py tests/test_private_* tests/test_project_scoped_checkpointer.py -q
uv run pytest tests/blocking_io -q
make lint
make format
git diff --check
```

Expected: 全部 PASS；format 后若文件变化，重新运行对应 focused tests 与 lint。

- [ ] **Step 5: 运行完整 backend 与 M1–M4 PostgreSQL gate**

Run:

```bash
cd backend
uv run pytest -q
uv run pytest tests/integration/test_m1_postgres_cutover.py tests/integration/test_project_isolation_postgres.py tests/integration/test_m2_project_governance_postgres.py tests/integration/test_m3_shared_assets_postgres.py tests/integration/test_m4_private_work_postgres.py tests/integration/test_m4_private_work_migration_postgres.py -q
```

Expected: 全部 PASS；PG gate不能 skip。

- [ ] **Step 6: 运行 frontend check、unit 与 Playwright**

Run:

```bash
cd frontend
pnpm check
pnpm test
pnpm exec playwright test
```

Expected: lint/typecheck、全部 unit、全部 E2E PASS。

- [ ] **Step 7: 运行 root/secret/log/doc consistency checks**

Run:

```bash
make check-db
make doctor
rg -n "3/8|37.5%|M4.*未开始|private work.*later milestone|M1, M2 and M3 PostgreSQL" README.md README_zh.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md docs .github/workflows
git diff --check
git status --short
```

Expected: DB/doctor通过；过期状态搜索无命中（历史完成文档中明确的旧里程碑叙述除外，并逐条人工确认）；diff check干净；status只含M4预期文件。

- [ ] **Step 8: 使用 `superpowers:requesting-code-review` 做独立 review**

review scope覆盖规格全部完成标准，重点检查：unscoped query/raw saver bypass、in-memory RunManager scope hit、secret persistence、file success ordering、migration fail-before-DDL、legacy cutover、frontend late response。任何 Critical/Important finding先修复，再从受影响 focused test开始重跑，并重复 Steps 4–7。

- [ ] **Step 9: 使用 `superpowers:verification-before-completion` 复核 fresh evidence**

不得引用旧输出。确认测试 exit code、skip count、PG database names、frontend check和git diff均来自当前 HEAD；随后才把总体设计 M4改为“已完成”、进度改为4/8，并将本计划 checkbox标记 `[x]`、顶部增加完成日期。

- [ ] **Step 10: 提交文档与完成状态**

```bash
git add README.md README_zh.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md docs .github/workflows/project-foundation-postgres-tests.yml
git commit -m "docs: complete M4 private work milestone"
```

- [ ] **Step 11: 进入分支收尾流程**

使用 `superpowers:finishing-a-development-branch` 展示 merge/PR/保留/清理选项；未经用户选择不自动 merge、push 或删除分支。

---

## Final acceptance checklist

- [ ] 所有私有根表的 project/owner 非空，所有父子关系有完整复合约束。
- [ ] 产品访问只使用 `PrivateWorkContext`、scoped repository 与 scoped checkpointer。
- [ ] 项目 Thread/run 复用现有 runtime，并保存 exact M3 asset/grant snapshot。
- [ ] credential/private content 在 M4 persistence 与日志面零泄漏。
- [ ] PostgreSQL 是 file/artifact authority，sandbox只作有 hash验证的临时投影。
- [ ] Memory queue与connection inbound捕获并重验真实 project-owner scope。
- [ ] membership/project revoke可跨 worker在下一副作用边界终止 run。
- [ ] staged migration、encrypted backup、ledger、probe、finalize和cutover marker顺序可验证、可幂等重跑。
- [ ] 项目 chats/Memory/connections/files及account/project cache隔离完整，Viewer与static-mode规则正确。
- [ ] legacy private API在cutover后只返回 `PRIVATE_WORK_CUTOVER`，不能读取project rows。
- [ ] M1–M4 PostgreSQL、完整 backend、frontend check/unit/Playwright全部fresh通过且无意外skip。
- [ ] README、AGENTS、总体设计、专项设计和运维runbook一致，独立review无Critical/Important finding。
