# M6 Project Reliability, Governance, and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把项目 Run 和 Automation 从单 Gateway 内存执行升级为 PostgreSQL job + 独立 Worker + 持久化 SSE，并补齐原子配额、只追加审计、平台运营面和可演练的加密备份恢复。

**Architecture:** Gateway 只做认证、作用域解析、事务 admission、入队、查询和 SSE；Worker 通过 PostgreSQL `FOR UPDATE SKIP LOCKED` 领取带 lease token 的 job，并复用唯一 `run_agent()` 生命周期；Scheduler 独立持有 M5 advisory lock，只创建 occurrence/job。SSE frame、quota ledger、audit、backup metadata 和 deletion tombstone 都以 PostgreSQL 或 operator-owned authenticated journal 为 authority，不引入 Redis/Kafka。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy async、Alembic、PostgreSQL、asyncpg、pytest/pytest-asyncio、cryptography AES-GCM、Next.js 16、React 19、TypeScript 5.8、TanStack Query 5、Zod、Rstest、Playwright、pnpm 10.26.2。

## Global Constraints

- PostgreSQL 是 job、lease、Run、stream、quota、audit 和 migration 的唯一在线 authority；禁止增加 SQLite、memory persistence、Redis、Kafka 或第二套 Agent executor。
- Gateway 不得创建 `run_agent()` task；M6 cutover 后只有独立 Worker 可以执行 Agent graph。
- API、Worker 和 Scheduler 是独立进程；Scheduler 同一时刻只允许一个持有既有 PostgreSQL session advisory lock 的 owner。
- 所有项目私有查询和 mutation 必须同时固定 `project_id + owner_user_id`，禁止裸 ID 查询后在应用内判断 scope。
- 客户端不能提供可信 project、owner、membership、role、capability、lease token、job type、asset version、credential grant、quota reservation 或 `non_interactive`。
- job 至少一次投递；重复 delivery 必须复用同一 Run 和 exact asset snapshot。不能证明外部副作用安全时固定进入 `SIDE_EFFECT_STATE_UNKNOWN` dead 状态，不自动重放。
- SSE frame 必须先写 `run_events(category='stream')` 再 notify；`Last-Event-ID` 只接受规范非负十进制游标，跨 Gateway 重连必须从 PostgreSQL 继续。
- 平台默认配额固定为：有效成员 20、项目存储 5 GiB、并发运行 3、MCP 调用每天 10,000 次；项目 Admin 只能收紧，不能突破平台有效值。
- quota counter 更新和 append-only usage ledger 必须同事务、同幂等键；硬限制拒绝下一个消耗操作但不中断已持 lease 的 Run。
- audit 只记录治理和运行元数据；禁止提示词、消息、Memory、运行日志、checkpoint、文件名/路径、附件、产物、credential 元数据、token、OAuth state、异常正文和 payload。
- `audit_logs` 与 `project_usage_ledger` 的已提交行由 PostgreSQL trigger 禁止 UPDATE/DELETE；更正只追加补偿行。
- `system_admin` 使用显式 system governance context，不能伪造 `ProjectContext`、owner 或读取任何用户私有内容。
- backup 使用独立 `DEER_FLOW_BACKUP_KEY` 和分块 AEAD；restore 只写新的空数据库，必须在开放服务前重放 archive high-watermark 之后的无间断 deletion tombstone journal。
- M6 使用 `0014_project_reliability_expand` → `0015_project_reliability_finalize`，maintenance-window、dry-run first、fail-before-DDL finalize 和 singleton cutover marker；marker 完成后只前向修复或恢复新库，不 downgrade。
- 真实 PostgreSQL 测试只创建随机 `deerflow_test_*` 数据库；CI 缺 `POSTGRES_TEST_URL` 必须在 pytest 前硬失败，M1–M6 release evidence 保持 0 skip。
- Backend 和 Frontend 均严格 TDD；每个任务先观察目标测试因缺少行为而失败，再写最小实现、运行受影响回归、更新相关 `AGENTS.md`，最后单独提交。
- M6 完成只把总体进度更新为 6/8（75%）；M7 legacy cleanup 和 M8 完整发布验收未完成时不得宣称完整多用户 SaaS。
- 现有用户改动不得覆盖；执行计划前使用 `superpowers:using-git-worktrees` 建立 `codex/m6-reliability-governance` 隔离工作区。

---

### Task 1: 建立 M6 final ORM、staged revisions 和 schema probes

**Files:**

- Create: `backend/packages/harness/deerflow/persistence/jobs/{__init__,model}.py`
- Create: `backend/packages/harness/deerflow/persistence/quotas/{__init__,model}.py`
- Create: `backend/packages/harness/deerflow/persistence/audit/{__init__,model}.py`
- Create: `backend/packages/harness/deerflow/persistence/recovery/{__init__,model}.py`
- Create: `backend/packages/harness/deerflow/persistence/reliability/{__init__,model}.py`
- Modify: `backend/packages/harness/deerflow/persistence/run/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/scheduled_task_runs/model.py`
- Modify: `backend/packages/harness/deerflow/persistence/models/__init__.py`
- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/0014_project_reliability_expand.py`
- Create: `backend/packages/harness/deerflow/persistence/migrations/versions/0015_project_reliability_finalize.py`
- Test: `backend/tests/test_m6_reliability_schema_postgres.py`
- Modify: `backend/tests/test_persistence_migrations_env.py`
- Modify: `backend/tests/test_default_project_bootstrap.py`

**Interfaces:**

- Produces ORM classes `JobRow`, `JobAttemptRow`, `DeadJobRow`, `WorkerNodeRow`, `ProjectQuotaRow`, `ProjectUsageCounterRow`, `ProjectUsageLedgerRow`, `AuditLogRow`, `DeletionTombstoneRow`, `RestoreProofRow`, `ReliabilityMigrationRunRow`, `ReliabilityMigrationLedgerRow`, `ReliabilityCutoverStateRow`。
- Adds nullable historical-safe `RunRow.job_id/execution_lease_token_hash/execution_lease_expires_at/execution_heartbeat_at/execution_started_at/cancel_requested_at/cancel_reason` and `ScheduledTaskRunRow.job_id`。
- `0014.down_revision == "0013_project_automation_finalize"`; `0015.down_revision == "0014_project_reliability_expand"`。

- [ ] **Step 1: 写 final catalog 和 fail-before-DDL RED tests**

```python
M6_TABLES = {
    "jobs", "job_attempts", "dead_jobs", "worker_nodes",
    "project_quotas", "project_usage_counters", "project_usage_ledger",
    "audit_logs", "deletion_tombstones", "restore_proofs",
    "reliability_migration_runs", "reliability_migration_ledger",
    "reliability_cutover_state",
}


async def test_m6_head_matches_final_metadata(migrated_postgres_database_url):
    engine = create_async_engine(migrated_postgres_database_url)
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        run_columns = await connection.run_sync(
            lambda sync: {c["name"]: c for c in inspect(sync).get_columns("runs")}
        )
    assert M6_TABLES <= tables
    assert revision == "0015_project_reliability_finalize"
    assert run_columns["job_id"]["nullable"] is True
    await engine.dispose()


def test_finalize_probes_before_schema_mutation():
    module = importlib.import_module(
        "deerflow.persistence.migrations.versions.0015_project_reliability_finalize"
    )
    source = inspect.getsource(module.upgrade)
    assert source.index("_assert_finalize_ready") < source.index("op.create_foreign_key")
```

同文件固定断言 job status/type/retry-safety CHECK、`(job_type,idempotency_key)` unique、attempt number unique、active lease indexes、Run/job 与 occurrence/job composite FK、ledger/audit append-only trigger、singleton marker CHECK、fresh `Base.metadata.create_all` 与 migration head equality。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m6_reliability_schema_postgres.py tests/test_persistence_migrations_env.py tests/test_default_project_bootstrap.py -q
```

Expected: FAIL with missing table `jobs`, missing revision module, or missing `RunRow.job_id`; PostgreSQL fixture不得以 skip 代替 RED。

- [ ] **Step 3: 实现 final ORM shape 并注册 metadata**

```python
class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(36))
    run_id: Mapped[str | None] = mapped_column(String(64))
    automation_occurrence_id: Mapped[str | None] = mapped_column(String(64))
    predecessor_dead_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    idempotency_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_token_hash: Mapped[str | None] = mapped_column(CHAR(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_safety: Mapped[str] = mapped_column(String(16), nullable=False, server_default="safe")
    public_error_code: Mapped[str | None] = mapped_column(String(64))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
```

其余 row 严格按专项规格第 6 节字段实现；`AuditLogRow` 保存 `target_ref_key_id + target_ref_hmac`，`DeletionTombstoneRow` 不保存 journal 明文坐标。

- [ ] **Step 4: 实现 staged revisions 和 probe-first finalize**

```python
revision = "0015_project_reliability_finalize"
down_revision = "0014_project_reliability_expand"


def upgrade() -> None:
    connection = op.get_bind()
    _assert_finalize_ready(connection)
    op.create_foreign_key("fk_runs_job", "runs", "jobs", ["job_id"], ["id"], ondelete="RESTRICT")
    _install_append_only_triggers(connection)
```

`_assert_finalize_ready` 在任何收紧 DDL 前验证 M4/M5 marker、active legacy execution 为零、quota backfill、job relation、trigger、stream 和 recovery probe；失败只输出固定公共原因。

- [ ] **Step 5: 验证 catalog 并提交**

```bash
cd backend
uv run pytest tests/test_m6_reliability_schema_postgres.py tests/test_persistence_migrations_env.py tests/test_default_project_bootstrap.py -q
git add packages/harness/deerflow/persistence tests/test_m6_reliability_schema_postgres.py tests/test_persistence_migrations_env.py tests/test_default_project_bootstrap.py AGENTS.md
git commit -m "feat: add M6 reliability schema"
```

Expected: PASS with revision `0015_project_reliability_finalize` and 0 PostgreSQL skips。

### Task 2: 建立 M6 config、errors、cutover 和 readiness contract

**Files:**

- Create: `backend/packages/harness/deerflow/config/{worker,quota,recovery}_config.py`
- Modify: `backend/packages/harness/deerflow/config/app_config.py`
- Modify: `backend/packages/harness/deerflow/config/reload_boundary.py`
- Modify: `config.example.yaml`
- Create: `backend/app/reliability/{__init__,models,errors,error_mapping,cutover,readiness}.py`
- Test: `backend/tests/test_m6_reliability_config.py`
- Test: `backend/tests/test_m6_reliability_cutover.py`
- Modify: `backend/tests/test_reload_boundary.py`

**Interfaces:**

- Produces `WorkerConfig`, `QuotaConfig`, `RecoveryConfig`。
- Produces `ReliabilityCutoverGuard.require_queue_open/require_gateway_open/require_worker_open/require_legacy_execution_open`。
- Produces `ReliabilityReadinessService.read() -> ReliabilityReadiness`。
- Stable errors map to 404/403/409/422/429/503 with `{code,message,request_id}` only。

- [ ] **Step 1: 写 validation 和 marker/revision RED tests**

```python
def test_worker_config_requires_heartbeat_inside_lease():
    with pytest.raises(ValidationError):
        WorkerConfig(lease_seconds=30, heartbeat_seconds=10)
    assert WorkerConfig(lease_seconds=31, heartbeat_seconds=10).max_concurrent_jobs == 4


async def test_cutover_requires_m4_m5_m6_marker(session_factory):
    guard = ReliabilityCutoverGuard(session_factory)
    with pytest.raises(ReliabilityCutover) as caught:
        await guard.require_gateway_open()
    assert caught.value.code == "RELIABILITY_CUTOVER"
```

固定 defaults：poll `0.5`、lease `90`、heartbeat `20`、concurrency `4`、shutdown grace `30`、attempts `3`、retry `2..300`；quota `20/5368709120/3/10000/0.8`；recovery chunk `1048576`。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m6_reliability_config.py tests/test_m6_reliability_cutover.py tests/test_reload_boundary.py -q
```

Expected: FAIL with `ModuleNotFoundError: deerflow.config.worker_config` or missing `ReliabilityCutoverGuard`。

- [ ] **Step 3: 实现严格 startup config**

```python
class WorkerConfig(BaseModel):
    enabled: bool = True
    poll_interval_seconds: float = Field(default=0.5, gt=0, le=30)
    lease_seconds: int = Field(default=90, ge=15, le=3600)
    heartbeat_seconds: int = Field(default=20, ge=1, le=1200)
    max_concurrent_jobs: int = Field(default=4, ge=1, le=128)
    shutdown_grace_seconds: int = Field(default=30, ge=1, le=600)
    default_max_attempts: int = Field(default=3, ge=1, le=20)
    retry_initial_seconds: int = Field(default=2, ge=1, le=3600)
    retry_max_seconds: int = Field(default=300, ge=1, le=86400)

    @model_validator(mode="after")
    def validate_intervals(self):
        if self.heartbeat_seconds * 3 >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be less than one third of lease_seconds")
        if self.retry_initial_seconds > self.retry_max_seconds:
            raise ValueError("retry_initial_seconds must not exceed retry_max_seconds")
        return self
```

在 `AppConfig` 和 `STARTUP_ONLY_FIELDS` 注册三段配置，bump `config_version`；example只写路径/数值，不写 key。

- [ ] **Step 4: 实现 errors、cutover 和 readiness**

```python
@dataclass(frozen=True)
class ReliabilityReadiness:
    status: Literal["ready", "degraded", "closed"]
    database: str
    schema: str
    worker_fleet: str
    scheduler: str
    stream: str
    recovery: str
    quota: str
    audit: str
    request_id: str
```

cutover每次从数据库读取 singleton marker，并通过 revision ancestry接受 future descendant；legacy execution只在 marker前开放，M6 path只在 final marker后开放。

- [ ] **Step 5: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_reliability_config.py tests/test_m6_reliability_cutover.py tests/test_reload_boundary.py -q
git add packages/harness/deerflow/config app/reliability tests/test_m6_reliability_config.py tests/test_m6_reliability_cutover.py tests/test_reload_boundary.py ../config.example.yaml AGENTS.md
git commit -m "feat: define M6 reliability contracts"
```

### Task 3: 实现 session-bound job repository 和 lease state machine

**Files:**

- Create: `backend/packages/harness/deerflow/persistence/jobs/sql.py`
- Create: `backend/app/reliability/jobs.py`
- Test: `backend/tests/test_m6_job_repository_postgres.py`
- Test: `backend/tests/test_m6_job_state_machine.py`

**Interfaces:**

- `JobRepository(session)` exposes `enqueue/claim_next/mark_running/heartbeat/request_cancel/settle_success/settle_cancelled/retry_or_dead/list_dead/requeue_safe`。
- Frozen `JobScope(project_id, owner_user_id)`、`EnqueueJob`、`JobClaim(job_id,attempt_id,lease_token,job_type,scope,run_id,occurrence_id,retry_safety,cancel_requested)`。
- Every owner transition compares SHA-256 lease token hash; stale owner returns `False`。

- [ ] **Step 1: 写并发 claim、stale token 和 dead successor RED tests**

```python
async def test_only_one_worker_claims_job(session_factory, seeded_job):
    async def claim(worker_id):
        async with session_factory() as session, session.begin():
            return await JobRepository(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=90,
            )
    first, second = await asyncio.gather(claim(uuid.uuid4()), claim(uuid.uuid4()))
    assert sum(item is not None for item in (first, second)) == 1


async def test_stale_lease_cannot_complete(repository, claimed_job):
    assert not await repository.settle_success(claimed_job.job_id, lease_token="wrong-token")
```

另测同幂等键返回同 job、attempt 单调、cancel-before-claim、expired safe reclaim、unsafe ambiguity dead、attempt exhausted dead、safe requeue创建 successor且不更新 dead projection。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m6_job_repository_postgres.py tests/test_m6_job_state_machine.py -q
```

Expected: FAIL with missing `JobRepository` / `JobClaim`。

- [ ] **Step 3: 实现 enqueue 和 SKIP LOCKED claim**

```python
@dataclass(frozen=True)
class EnqueueJob:
    job_type: Literal["private_run", "automation_run", "retention_purge"]
    scope: JobScope
    idempotency_key: str
    run_id: str | None
    occurrence_id: str | None
    max_attempts: int
    retry_safety: Literal["safe", "unknown", "unsafe"] = "safe"
```

`claim_next` 使用 `status in queued/retry_wait/expired leased/running`、`available_at <= now()`、capability filter、priority/available/created/id排序与 `with_for_update(skip_locked=True)`；同事务生成 32-byte token、只存 hash、创建 attempt并设置 lease。

- [ ] **Step 4: 实现 token-guarded transitions 和 retry/dead**

`retry_or_dead` 只重试 `safe` 且 attempts剩余的 job，backoff为 `min(max, initial * 2 ** (attempt_count - 1))`；unknown/unsafe或exhausted同事务写 immutable `dead_jobs`。`requeue_safe` 要求 dead row safe，创建 `predecessor_dead_job_id` successor和新 audit port event。

- [ ] **Step 5: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_job_repository_postgres.py tests/test_m6_job_state_machine.py -q
git add packages/harness/deerflow/persistence/jobs app/reliability/jobs.py tests/test_m6_job_repository_postgres.py tests/test_m6_job_state_machine.py AGENTS.md
git commit -m "feat: add durable job leases"
```

Expected: PASS with 20 repeated concurrent claims and 0 skips。

### Task 4: 建立 Worker registry、claim loop、heartbeat 和 drain lifecycle

**Files:**

- Create: `backend/app/reliability/workers.py`
- Create: `backend/app/worker/{__init__,service,app}.py`
- Modify: `backend/Makefile`
- Test: `backend/tests/test_m6_worker_registry_postgres.py`
- Test: `backend/tests/test_m6_worker_service.py`
- Test: `backend/tests/blocking_io/test_m6_worker_loop.py`

**Interfaces:**

- `WorkerRegistry.register/heartbeat/mark_draining/remove/has_fresh_capability`。
- `WorkerService(repository_factory, registry, handlers, config).run(stop_event)` and `.drain()`。
- Handler protocol `async def handle(claim: JobClaim, authority: JobLeaseAuthority) -> JobOutcome`。

- [ ] **Step 1: 写 fleet freshness、bounded concurrency 和 drain RED tests**

```python
async def test_worker_service_never_exceeds_configured_concurrency(fake_repo):
    active = peak = 0
    async def handler(claim, authority):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return JobOutcome.succeeded()
    service = WorkerService(fake_repo.factory, fake_repo.registry, {"private_run": handler}, WorkerConfig(max_concurrent_jobs=2))
    await service.run_until_idle()
    assert peak == 2
```

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m6_worker_registry_postgres.py tests/test_m6_worker_service.py tests/blocking_io/test_m6_worker_loop.py -q
```

Expected: FAIL with missing `WorkerService` / `WorkerRegistry`。

- [ ] **Step 3: 实现 registry 和 lease authority**

```python
class JobLeaseAuthority:
    async def heartbeat(self) -> None:
        async with self._factory() as session, session.begin():
            ok = await JobRepository(session).heartbeat(
                self.claim.job_id,
                lease_token=self.claim.lease_token,
                lease_seconds=self._lease_seconds,
            )
        if not ok:
            raise LeaseLost(self.claim.job_id)
```

registry只保存随机 Worker UUID、版本、capabilities、capacity、started/heartbeat/draining，不保存 hostname/env/URL。

- [ ] **Step 4: 实现 poll、per-job heartbeat 和 graceful drain**

```python
async def run(self, stop_event: asyncio.Event) -> None:
    await self._registry.register(self.worker_id, frozenset(self._handlers), self._config.max_concurrent_jobs)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._fleet_heartbeat(stop_event))
            group.create_task(self._claim_loop(stop_event))
    finally:
        await self._registry.mark_draining(self.worker_id)
        await self._drain_inflight(self._config.shutdown_grace_seconds)
        await self._registry.remove(self.worker_id)
```

同步文件/crypto/subprocess通过 `asyncio.to_thread` 或 async subprocess，blocking-I/O test必须实际触达。

- [ ] **Step 5: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_worker_registry_postgres.py tests/test_m6_worker_service.py tests/blocking_io/test_m6_worker_loop.py -q
git add app/reliability/workers.py app/worker Makefile tests/test_m6_worker_registry_postgres.py tests/test_m6_worker_service.py tests/blocking_io/test_m6_worker_loop.py AGENTS.md
git commit -m "feat: add independent worker lifecycle"
```

Expected: PASS；stop后不再claim，grace超时保留可接管 lease。

### Task 5: 将 private Run admission 改为 Run + job 原子事务

**Files:**

- Create: `backend/app/private_work/run_admission.py`
- Create: `backend/app/reliability/jobs.py`
- Modify: `backend/app/gateway/services.py`
- Modify: `backend/app/gateway/routers/private_work.py`
- Test: `backend/tests/test_m6_private_run_admission_postgres.py`
- Modify: `backend/tests/test_project_private_run_gateway.py`

**Interfaces:** `PrivateRunAdmission.admit(context, request) -> AdmittedPrivateRun` 在同一 session 中锁定 quota counter、创建唯一 Run、创建 `private_run` job、写 usage/audit；`GatewayServices.start_private_run` 只调用 admission，不创建 asyncio task。

- [ ] **Step 1: 写 admission RED tests**

```python
async def test_admit_run_and_job_is_atomic(private_scope, admission):
    result = await admission.admit(private_scope, RunAdmissionRequest(message="hello"))
    assert result.job.run_id == result.run.id
    assert result.job.owner_user_id == private_scope.owner_user_id
    assert result.job.idempotency_key == sha256(f"private_run:{result.run.id}".encode()).hexdigest()

async def test_gateway_never_launches_local_agent(gateway_services, monkeypatch):
    monkeypatch.setattr(asyncio, "create_task", Mock(side_effect=AssertionError("local launch")))
    await gateway_services.start_private_run(authenticated_request())
```

同文件覆盖事务回滚不留孤儿 Run/job、重试返回同一 pair、Viewer 禁止写、伪造 scope/non_interactive 被丢弃、quota 拒绝无部分写入。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_private_run_admission_postgres.py tests/test_project_private_run_gateway.py -q
```

Expected: FAIL because `PrivateRunAdmission` 不存在且 Gateway 仍走 local `RunManager`。

- [ ] **Step 3: 写最小原子 admission**

```python
class PrivateRunAdmission:
    async def admit(self, context: ProjectContext, request: RunAdmissionRequest) -> AdmittedPrivateRun:
        async with self._sessions.begin() as session:
            run = await self._runs.create_pending(session, context, request)
            job = await self._jobs.enqueue_private_run(session, context, run)
            await self._runs.attach_job(session, context, run.id, job.id)
            await self._quota.reserve_concurrent_run(session, context, run.id)
            await self._audit.run_admitted(session, context, run.id, job.id)
        return AdmittedPrivateRun(run=run, job=job)
```

删除 Gateway 内 `run_agent()`/`asyncio.create_task()` launch path；HTTP 返回既有 Run contract，job ID 不成为客户端 authority。

- [ ] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_private_run_admission_postgres.py tests/test_project_private_run_gateway.py -q
git add app/private_work/run_admission.py app/reliability/jobs.py app/gateway/services.py app/gateway/routers/private_work.py tests/test_m6_private_run_admission_postgres.py tests/test_project_private_run_gateway.py AGENTS.md
git commit -m "feat: enqueue private runs transactionally"
```

Expected: PASS；Gateway process 内不存在 Agent executor。

### Task 6: 将 private Run execution、cancel 和 checkpoint takeover 收敛到 Worker

**Files:**

- Create: `backend/app/reliability/execution.py`
- Modify: `backend/app/private_work/run_service.py`
- Modify: `backend/app/private_work/run_repository.py`
- Modify: `backend/packages/harness/deerflow/agents/worker.py`
- Modify: `backend/app/worker/service.py`
- Test: `backend/tests/test_m6_private_run_worker_postgres.py`
- Test: `backend/tests/test_m6_private_run_cancel_postgres.py`

**Interfaces:** `PrivateRunJobHandler.execute(claim: ClaimedJob) -> JobSettlement` 是唯一调用 `run_agent()` 的 M6 adapter；每次 Run state mutation 同时校验 `project_id + owner_user_id + job_id + lease_token_hash`。

- [ ] **Step 1: 写 execution authority RED tests**

```python
async def test_stale_lease_cannot_complete_run(handler, expired_claim):
    with pytest.raises(JobLeaseLost):
        await handler.complete(expired_claim, AgentResult.success())

async def test_retry_reuses_run_and_checkpoint(handler, reclaimed_job):
    result = await handler.execute(reclaimed_job)
    assert result.run_id == reclaimed_job.run_id
    assert result.checkpoint_namespace == reclaimed_job.run_id
```

覆盖 cancel queued、cancel leased cooperative stop、terminal幂等、safe transient retry、unsafe ambiguous side effect 进入 `SIDE_EFFECT_STATE_UNKNOWN`、lease 丢失不得发布 terminal。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_private_run_worker_postgres.py tests/test_m6_private_run_cancel_postgres.py -q
```

Expected: FAIL with missing handler/lease guard。

- [ ] **Step 3: 实现 Worker-only handler**

```python
async def execute(self, claim: ClaimedJob) -> JobSettlement:
    run = await self._runs.begin_execution(claim.scope, claim.run_id, claim.lease_proof)
    try:
        result = await run_agent(run.context, checkpoint_id=run.id)
        return await self._settle_success(claim, result)
    except AmbiguousExternalSideEffect:
        return await self._settle_dead(claim, "SIDE_EFFECT_STATE_UNKNOWN")
    except TransientExecutionError as exc:
        return await self._settle_retry(claim, public_code(exc))
```

cancel flag由 heartbeat/cancellation checkpoint 读取；Worker stop只释放安全边界，不伪造 terminal。

- [ ] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_private_run_worker_postgres.py tests/test_m6_private_run_cancel_postgres.py tests/test_project_private_run_gateway.py -q
git add app/reliability/execution.py app/private_work/run_service.py app/private_work/run_repository.py app/worker/service.py packages/harness/deerflow/agents/worker.py tests/test_m6_private_run_worker_postgres.py tests/test_m6_private_run_cancel_postgres.py AGENTS.md
git commit -m "feat: execute private runs in worker"
```

Expected: PASS；stale Worker 无法改变 Run/job 终态。

### Task 7: 将 Automation occurrence、Run 和 job 原子化并拆出 Scheduler 进程

**Files:**

- Create: `backend/app/scheduler/{__init__,app,service}.py`
- Modify: `backend/app/automations/dispatcher.py`
- Modify: `backend/app/automations/occurrences.py`
- Modify: `backend/app/automations/reconciliation.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/Makefile`
- Test: `backend/tests/test_m6_automation_job_admission_postgres.py`
- Test: `backend/tests/test_m6_scheduler_process.py`
- Modify: `backend/tests/test_automation_scheduler_postgres.py`

**Interfaces:** `AutomationDispatcher.admit_occurrence()` 在一个事务创建唯一 occurrence + Run + job；`SchedulerApp` 独占既有 session advisory lock，Gateway lifespan 不再启动 poller；restart reconciliation 只协调已 admitted 终态。

- [ ] **Step 1: 写 RED tests**

```python
async def test_occurrence_run_job_commit_together(dispatcher, due_definition):
    admitted = await dispatcher.admit_occurrence(due_definition, scheduled_for=NOW)
    assert admitted.occurrence.run_id == admitted.run.id
    assert admitted.run.job_id == admitted.job.id

def test_gateway_lifespan_does_not_construct_scheduler(monkeypatch):
    monkeypatch.setattr("app.scheduler.service.SchedulerService", Mock(side_effect=AssertionError))
    with TestClient(create_gateway_app()):
        pass
```

覆盖唯一 occurrence 重试、手动触发、锁竞争、ownership_lost fail-stop、disabled scheduler、crash 后不自动 replay admitted Run。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_automation_job_admission_postgres.py tests/test_m6_scheduler_process.py tests/test_automation_scheduler_postgres.py -q
```

Expected: FAIL because M5 Scheduler 仍由 Gateway lifespan 托管。

- [ ] **Step 3: 拆分 Scheduler 并接 job admission**

```python
async def scheduler_main() -> None:
    service = SchedulerService.from_config(load_app_config())
    async with service.process_lifetime_lock():
        await service.reconcile_admitted_occurrences()
        await service.poll_forever()
```

保留同一物理 PostgreSQL session/PID/lock ownership 验证；poller只 enqueue，禁止 import Worker executor。

- [ ] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_automation_job_admission_postgres.py tests/test_m6_scheduler_process.py tests/test_automation_scheduler_postgres.py -q
git add app/scheduler app/automations app/gateway/app.py Makefile tests/test_m6_automation_job_admission_postgres.py tests/test_m6_scheduler_process.py tests/test_automation_scheduler_postgres.py AGENTS.md
git commit -m "feat: run automations through jobs"
```

Expected: PASS；Gateway、Worker、Scheduler 生命周期互不内嵌。

### Task 8: 建立 PostgreSQL durable stream writer、reader 和 terminal invariant

**Files:**

- Create: `backend/packages/harness/deerflow/runtime/stream_bridge/postgres.py`
- Modify: `backend/packages/harness/deerflow/runtime/events/store/base.py`
- Modify: `backend/packages/harness/deerflow/runtime/events/store/db.py`
- Modify: `backend/packages/harness/deerflow/runtime/events/models.py`
- Modify: `backend/app/reliability/execution.py`
- Test: `backend/tests/test_m6_durable_stream_postgres.py`

**Interfaces:** `PostgresStreamBridge.publish()` 先持久化 frame 后发送 best-effort notify；`read_after(thread_id, owner, cursor, limit)` 严格 scope；每个 Run 只有一个 terminal frame，event id 为 thread 单调十进制序号。

- [ ] **Step 1: 写 durable stream RED tests**

```python
async def test_frames_survive_bridge_restart(stream_factory, scoped_thread):
    first = await stream_factory().publish(scoped_thread, StreamFrame.data({"delta": "A"}))
    reopened = stream_factory()
    page = await reopened.read_after(scoped_thread.scope, scoped_thread.id, cursor=0, limit=50)
    assert [frame.id for frame in page] == [first.id]

async def test_terminal_is_unique_under_race(stream, run):
    results = await asyncio.gather(*[stream.publish_terminal(run, "completed") for _ in range(2)])
    assert len({item.id for item in results}) == 1
```

覆盖 rollback 不 notify、跨 project/owner 404、gap-free pagination、cursor 超界、unknown category 拒绝、Worker retry不重复 terminal。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_durable_stream_postgres.py -q
```

Expected: FAIL because current bridge 不提供 PostgreSQL replay contract。

- [ ] **Step 3: 实现 store-first bridge**

```python
async def publish(self, scope: PrivateScope, thread_id: str, frame: StreamFrame) -> StoredFrame:
    async with self._sessions.begin() as session:
        stored = await self._events.append_stream_frame(session, scope, thread_id, frame)
    await self._notifier.best_effort_notify(thread_id, stored.id)
    return stored
```

terminal 使用数据库 unique constraint/upsert 返回既有行；notify 只降低延迟，不影响 correctness。

- [ ] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_durable_stream_postgres.py tests/test_run_event_store.py -q
git add packages/harness/deerflow/runtime/stream_bridge/postgres.py packages/harness/deerflow/runtime/events app/reliability/execution.py tests/test_m6_durable_stream_postgres.py AGENTS.md
git commit -m "feat: persist private run streams"
```

Expected: PASS；重启或换 Gateway 后可从同一 cursor 继续。

### Task 9: 接入 Gateway SSE replay 和 Frontend cursor/dedupe

**Files:**

- Modify: `backend/app/gateway/routers/private_work.py`
- Modify: `backend/app/gateway/deps.py`
- Create: `backend/tests/test_m6_private_sse_reconnect_postgres.py`
- Modify: `frontend/src/core/private-work/client.ts`
- Modify: `frontend/src/core/private-work/registry.ts`
- Modify: `frontend/src/core/threads/hooks.ts`
- Test: `frontend/tests/m6-private-stream-reconnect.test.ts`
- Test: `frontend/tests/m6-private-stream-cache-isolation.test.ts`

**Interfaces:** SSE endpoint接受标准 `Last-Event-ID` header；仅规范非负十进制，空值等同 0；服务端 replay DB 后等待 notify/poll，再次读取 DB；前端按 `{accountId, projectId, threadId}` 保存 cursor 并按 event id 去重。

- [ ] **Step 1: 写 backend/frontend RED tests**

```python
@pytest.mark.parametrize("cursor", ["-1", "+1", "01", "1.0", " 1", "abc"])
async def test_rejects_noncanonical_last_event_id(client, cursor):
    response = await client.get(STREAM_URL, headers={"Last-Event-ID": cursor})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_STREAM_CURSOR"
```

```ts
it("dedupes replayed terminal frames after reconnect", async () => {
  const state = reduceFrames(emptyState(), [frame(7, "terminal"), frame(7, "terminal")]);
  expect(state.terminalCount).toBe(1);
  expect(state.lastEventId).toBe(7);
});
```

覆盖跨 account/project cache 隔离、429/503 retry-after、terminal 后不重连、route gate/readiness、静态构建无未授权入口。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_private_sse_reconnect_postgres.py -q
cd ../frontend
pnpm test -- m6-private-stream-reconnect.test.ts m6-private-stream-cache-isolation.test.ts
```

Expected: backend 或 frontend 至少一个因缺 cursor replay/dedupe 失败。

- [ ] **Step 3: 实现 reconnect contract**

```ts
export function streamKey(scope: PrivateScope, threadId: string) {
  return ["private-stream", scope.accountId, scope.projectId, threadId] as const;
}

export function acceptFrame(state: StreamState, frame: ServerFrame): StreamState {
  if (frame.id <= state.lastEventId) return state;
  return applyNewFrame(state, frame);
}
```

SSE response 每帧写 `id: {seq}`；断线恢复只从 server-confirmed cursor 开始，禁止客户端推断缺帧。

- [ ] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_private_sse_reconnect_postgres.py -q
cd ../frontend
pnpm test -- m6-private-stream-reconnect.test.ts m6-private-stream-cache-isolation.test.ts
pnpm check
git add ../backend/app/gateway/routers/private_work.py ../backend/app/gateway/deps.py ../backend/tests/test_m6_private_sse_reconnect_postgres.py src/core/private-work src/core/threads/hooks.ts tests/m6-private-stream-reconnect.test.ts tests/m6-private-stream-cache-isolation.test.ts AGENTS.md
git commit -m "feat: reconnect private streams durably"
```

Expected: PASS；浏览器刷新、Gateway 重启和跨实例重连均不丢帧、不重复 terminal。

### Task 10: 建立 quota policy、原子 counter、append-only ledger 和 reconciliation

**Files:**

- Create: `backend/app/quotas/{__init__,models,service,reconciliation}.py`
- Create: `backend/packages/harness/deerflow/persistence/quotas/sql.py`
- Test: `backend/tests/test_m6_quota_service_postgres.py`
- Test: `backend/tests/test_m6_quota_reconciliation_postgres.py`

**Interfaces:** `QuotaService.reserve/consume(session, issued_context, resource, amount, idempotency_key)`；`release` 只接受 module-issued、reason→dimension 固定且匹配原 reservation 的 compensation authority；effective limit 为平台默认与项目收紧值的最小值；counter 与 ledger 同事务，ordinary/policy/reconcile 的 80% threshold 各 bucket 只追加一次。

- [x] **Step 1: 写 RED tests**

```python
async def test_concurrent_reservations_never_exceed_limit(quota, project_scope):
    results = await asyncio.gather(*[
        quota.reserve_new_session(project_scope, "concurrent_runs", 1, f"run:{i}")
        for i in range(6)
    ], return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 3

async def test_same_idempotency_key_writes_one_ledger_row(quota, scope):
    await quota.consume_new_session(scope, "mcp_calls_daily", 1, "call:42")
    await quota.consume_new_session(scope, "mcp_calls_daily", 1, "call:42")
    assert await ledger_count("call:42") == 1
```

覆盖 20 members、5 GiB、3 runs、10,000 MCP/day 默认值，Admin只能收紧，日窗 UTC，release补偿行，trigger拒绝 UPDATE/DELETE，reconcile dry-run/execute。

- [x] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_quota_service_postgres.py tests/test_m6_quota_reconciliation_postgres.py -q
```

Expected: FAIL with missing quota repository/service。

- [x] **Step 3: 实现原子 reserve**

```python
async def reserve(self, session, context, resource, amount, key):
    await self._lock_current_authority(session, context)
    counter = await self._repo.lock_counter(session, context.project_id, resource, current_window(resource))
    limit = await self._repo.effective_limit(session, context.project_id, resource)
    if counter.used + counter.reserved + amount > limit:
        raise QuotaExceeded(resource)
    return await self._repo.append_and_apply(session, counter, amount, key, operation="reserve")
```

reconciliation 从 authoritative rows 计算 expected，dry-run只报差异；execute追加 `reconcile_adjustment` ledger 后修正 counter。

- [x] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_quota_service_postgres.py tests/test_m6_quota_reconciliation_postgres.py -q
git add app/quotas packages/harness/deerflow/persistence/quotas/sql.py tests/test_m6_quota_service_postgres.py tests/test_m6_quota_reconciliation_postgres.py AGENTS.md
git commit -m "feat: add atomic project quotas"
```

Expected: PASS under concurrent PostgreSQL sessions。

### Task 11: 在 member、storage、Run 和 MCP 边界执行 quota

**Files:**

- Modify: `backend/app/gateway/routers/project_members.py`
- Modify: `backend/app/private_work/run_admission.py`
- Modify: `backend/app/private_work/run_service.py`
- Modify: `backend/app/private_work/file_service.py`
- Modify: `backend/packages/harness/deerflow/tools/mcp_tool.py`
- Test: `backend/tests/test_m6_quota_integration_postgres.py`
- Modify: `backend/tests/test_project_member_governance_postgres.py`
- Modify: `backend/tests/test_project_private_files_postgres.py`

**Interfaces:** 成员邀请/加入前 reserve member；文件 finalize 前 reserve storage，删除后 release；Run admission reserve concurrent，任何 terminal只 release一次；每个实际 MCP dispatch 前 consume daily call。

- [x] **Step 1: 写 integration RED tests**

```python
async def test_run_quota_released_once_after_worker_retry(admit_three_runs, worker):
    run = await admit_three_runs.last()
    await worker.retry_then_complete(run.job_id)
    assert await reserved("concurrent_runs") == 2

async def test_storage_rejects_finalize_without_orphaning_blob(upload_over_limit):
    response = await upload_over_limit.finalize()
    assert response.code == "PROJECT_STORAGE_QUOTA_EXCEEDED"
    assert not await upload_over_limit.has_visible_file()
```

覆盖 hard-limit stable error/Retry-After、80% event、member并发 race、MCP失败也计实际 dispatch、正在运行任务不被新下调中断。

- [x] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_quota_integration_postgres.py tests/test_project_member_governance_postgres.py tests/test_project_private_files_postgres.py -q
```

Expected: FAIL because domain mutations尚未调用 quota port。

- [x] **Step 3: 把 reserve/consume 纳入现有事务**

```python
async with sessions.begin() as session:
    await quotas.reserve(session, context, "project_storage_bytes", upload.size, f"file:{upload.id}")
    file = await files.finalize(session, context, upload)
```

所有 idempotency key 由 server authority ID 派生；不得接受客户端 key 或在 commit 后补记 ledger。

- [x] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_quota_integration_postgres.py tests/test_project_member_governance_postgres.py tests/test_project_private_files_postgres.py -q
git add app/gateway/routers/project_members.py app/private_work packages/harness/deerflow/tools/mcp_tool.py tests/test_m6_quota_integration_postgres.py tests/test_project_member_governance_postgres.py tests/test_project_private_files_postgres.py AGENTS.md
git commit -m "feat: enforce project quota boundaries"
```

Expected: PASS；无 counter/ledger/domain write 分裂。

### Task 12: 建立 audit whitelist、HMAC target ref 和 append-only repository

**Files:**

- Create: `backend/app/audit/{__init__,models,service}.py`
- Create: `backend/packages/harness/deerflow/persistence/audit/sql.py`
- Test: `backend/tests/test_m6_audit_service_postgres.py`
- Test: `backend/tests/test_m6_audit_redaction.py`

**Interfaces:** `AuditService.append(session, actor, action: AuditAction, target: AuditTarget, outcome, metadata)` 只接受枚举 action 和每 action 固定 metadata schema；private target 保存 key id + HMAC，不保存名称/路径/内容。

- [x] **Step 1: 写 audit RED tests**

```python
@pytest.mark.parametrize("forbidden", ["prompt", "message", "memory", "path", "filename", "token", "exception"])
async def test_metadata_rejects_private_fields(audit, forbidden):
    with pytest.raises(AuditMetadataRejected):
        await audit.append_new_session(event(metadata={forbidden: "secret"}))

async def test_committed_audit_rows_are_immutable(pg_session, audit_row):
    with pytest.raises(DBAPIError):
        await pg_session.execute(update(AuditLogRow).where(AuditLogRow.id == audit_row.id).values(outcome="changed"))
```

覆盖 project reader双 scope、system reader governance context、key rotation lookup、stable public errors、无异常正文。

- [x] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_audit_service_postgres.py tests/test_m6_audit_redaction.py -q
```

Expected: FAIL with missing audit contracts。

- [x] **Step 3: 实现 allowlist-first append**

```python
async def append(self, session, actor, action, target, outcome, metadata):
    sanitized = AUDIT_METADATA_MODELS[action].model_validate(metadata).model_dump(mode="json")
    target_hmac = self._refs.digest(target.authority_id)
    return await self._repo.insert(session, actor, action.value, target_hmac, outcome, sanitized)
```

Pydantic models 使用 `extra="forbid"`；HMAC key 与 backup/credential key 分离，日志不得输出原 target。

- [x] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_audit_service_postgres.py tests/test_m6_audit_redaction.py -q
git add app/audit packages/harness/deerflow/persistence/audit/sql.py tests/test_m6_audit_service_postgres.py tests/test_m6_audit_redaction.py AGENTS.md
git commit -m "feat: add privacy-safe audit log"
```

Expected: PASS；已提交 audit 行不可修改或删除。

### Task 13: 将 audit sink 接入治理、运行、job 和 recovery

**Files:**

- Create: `backend/app/audit/sinks.py`
- Modify: `backend/app/shared_assets/audit.py`
- Modify: `backend/app/gateway/routers/project_members.py`
- Modify: `backend/app/private_work/run_admission.py`
- Modify: `backend/app/reliability/{jobs,execution}.py`
- Modify: `backend/app/automations/dispatcher.py`
- Modify: `backend/app/recovery/archive.py`
- Test: `backend/tests/test_m6_audit_integration_postgres.py`
- Modify: `backend/tests/test_asset_audit_sink.py`

**Interfaces:** 保持 M3 `AssetAuditSink` port兼容，adapter写入新 audit repository；治理 mutation、admit/cancel/terminal/dead/requeue、Automation trigger、quota policy、backup/restore/purge 均在业务事务或 trusted-operation事务写 audit。

- [x] **Step 1: 写 sink RED tests**

```python
async def test_failed_member_removal_writes_no_success_audit(remove_last_admin):
    response = await remove_last_admin()
    assert response.status_code == 409
    assert not await audit_exists(action="member.removed", outcome="success")

async def test_dead_job_audit_contains_codes_not_exception(dead_job):
    row = await audit_for(dead_job)
    assert row.metadata == {"job_type": "private_run", "public_error_code": "SIDE_EFFECT_STATE_UNKNOWN"}
```

覆盖同事务回滚、M3 sink regression、system operation actor、禁止 payload 泄漏。

- [x] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_audit_integration_postgres.py tests/test_asset_audit_sink.py -q
```

Expected: FAIL because sinks尚未接入新 repository。

- [x] **Step 3: 接入 typed sinks 并验证**

完成并通过三次独立修复审查（2026-07-17）：Task 13 的 process actor 除了 context identity 外还绑定所属 `AuditService` issuer，临时 service 签发的 actor 不能交给真实 service append；generic process binder 与任意-role test binder 已移除，生产仅保留固定 Gateway/Worker/Scheduler/Operator/Recovery composition 路径。Gateway 的 project、Automation 与 shared-assets mutation audit dependency 缺失时统一 fail-closed 503，其中 Automation 通过专属映射返回稳定 `AUTOMATION_UNAVAILABLE/message/request_id` envelope，project/shared-assets 保持通用 503；兼容 router tests 显式注入 test sink。Project create/update、删除/恢复/暂停/恢复运行、邀请 create/revoke/redeem+member join、Automation create/update/pause/resume/delete 与既有 governance/run/job mutation 都在 caller-owned 业务事务中写 audit，失败时与领域 mutation 一起回滚；same-role membership update 不增 version、不写 audit。safe requeue 不再存在 module signer；repository 在同一 session 中锁定并重验 exact dead+safe predecessor 与刚创建的 queued safe successor后，才内联签发不含 owner/run 的一次性 event；首次 sink 校验原子消费 capability，callback retain 后抛错也由 repository `finally` 撤销，事务重试必须重新创建 successor 并签发新 event。`PrivateRunRepository.create()` 与 `settle_execution()` 返回注解已分别固定为 `PrivateRunRecord` 与 `PrivateRunSettlement`。production Worker dead settlement 通过显式 terminal-publication result 建立单一 audit owner，固定只写一条 `job.dead` 与一条 `run.terminal`，错误码统一为 `SIDE_EFFECT_STATE_UNKNOWN`。Task 14 quota-policy HTTP mutation 与 Tasks 16–17 backup/restore/purge caller 尚不存在且继续由各自任务创建；Task 13 只提供同事务 typed contracts，不创建 recovery placeholder module。第三次修复 fresh gates：audit/requeue 71 passed，governance/Automation 195 passed（1 个既有 deprecation warning），job/private-run/Worker/Scheduler 93 passed，shared-assets 205 passed（1 个既有 warning），harness boundary 7 passed。

```bash
cd backend
uv run pytest tests/test_m6_audit_integration_postgres.py tests/test_asset_audit_sink.py -q
git add app/audit/sinks.py app/shared_assets/audit.py app/gateway/routers/project_members.py app/private_work/run_admission.py app/reliability app/automations/dispatcher.py app/recovery/archive.py tests/test_m6_audit_integration_postgres.py tests/test_asset_audit_sink.py AGENTS.md
git commit -m "feat: audit project reliability operations"
```

Expected: PASS；成功/拒绝/失败事件与领域提交一致。

### Task 14: 提供 project Admin usage/audit API 和 UI

**Files:**

- Create: `backend/app/gateway/routers/{project_usage,project_audit}.py`
- Modify: `backend/app/gateway/app.py`
- Test: `backend/tests/test_m6_project_governance_api_postgres.py`
- Create: `frontend/src/core/project-governance/{usage,audit}.ts`
- Create: `frontend/src/components/projects/governance/{project-usage-page,project-audit-page}.tsx`
- Create: `frontend/src/app/projects/[project_slug]/settings/usage/page.tsx`
- Create: `frontend/src/app/projects/[project_slug]/settings/audit/page.tsx`
- Modify: `frontend/src/components/projects/project-sidebar.tsx`
- Test: `frontend/tests/m6-project-governance.test.tsx`

**Interfaces:** Admin可读 effective limits/current usage/80% state、收紧 project quota、分页读本项目 audit；Member/Viewer 404；query key始终含 account+project；页面受 server capability + M6 readiness gate。

- [ ] **Step 1: 写 API/UI RED tests**

```ts
it("does not expose governance links without server capability", () => {
  renderSidebar({ capabilities: [] });
  expect(screen.queryByText("Usage")).not.toBeInTheDocument();
  expect(screen.queryByText("Audit")).not.toBeInTheDocument();
});
```

backend覆盖 role、双 scope、cursor、limit收紧、超平台值拒绝；frontend覆盖 cache isolation、loading/empty/error、secret field absence、静态构建门禁。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_project_governance_api_postgres.py -q
cd ../frontend
pnpm test -- m6-project-governance.test.tsx
```

Expected: routes/components missing。

- [ ] **Step 3: 实现 strict scoped API/UI 并验证**

```bash
cd backend
uv run pytest tests/test_m6_project_governance_api_postgres.py -q
cd ../frontend
pnpm test -- m6-project-governance.test.tsx
pnpm check
git add ../backend/app/gateway ../backend/tests/test_m6_project_governance_api_postgres.py src/core/project-governance src/components/projects/governance src/app/projects/[project_slug]/settings src/components/projects/project-sidebar.tsx tests/m6-project-governance.test.tsx AGENTS.md
git commit -m "feat: add project governance console"
```

Expected: PASS；无 capability/readiness 时 route 和 sidebar 都不暴露入口。

### Task 15: 提供 system_admin operations/projects/jobs/audit API 和 UI

**Files:**

- Create: `backend/app/gateway/routers/{admin_operations,admin_projects,admin_jobs,admin_audit}.py`
- Modify: `backend/app/gateway/app.py`
- Test: `backend/tests/test_m6_system_operations_api_postgres.py`
- Create: `frontend/src/core/admin-operations/{types,api,query-keys}.ts`
- Create: `frontend/src/components/admin/operations/{admin-operations-shell,operations-overview,admin-projects,admin-jobs,admin-audit}.tsx`
- Create: `frontend/src/app/admin/{operations,projects,jobs,audit}/page.tsx`
- Modify: `frontend/src/app/admin/layout.tsx`
- Test: `frontend/tests/m6-admin-operations.test.tsx`

**Interfaces:** system_admin仅获取健康度、聚合使用量、project ID/status、job type/status/public code、审计元数据；dead job requeue要求显式 safe eligibility + predecessor link；不得读取 owner私有内容或伪造 ProjectContext。

- [ ] **Step 1: 写 privacy boundary RED tests**

```python
async def test_admin_job_response_has_no_private_fields(system_admin_client, private_dead_job):
    body = (await system_admin_client.get("/api/admin/jobs")).json()
    encoded = json.dumps(body)
    for forbidden in ("prompt", "message", "thread_title", "file_name", "owner_email", "exception"):
        assert forbidden not in encoded
```

覆盖非 admin 404、聚合 pagination、unsafe requeue拒绝、new job predecessor、audit system context、frontend server layout和cache key。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_system_operations_api_postgres.py -q
cd ../frontend
pnpm test -- m6-admin-operations.test.tsx
```

Expected: routes/components missing。

- [ ] **Step 3: 实现最小运营面并验证**

```bash
cd backend
uv run pytest tests/test_m6_system_operations_api_postgres.py -q
cd ../frontend
pnpm test -- m6-admin-operations.test.tsx
pnpm check
git add ../backend/app/gateway ../backend/tests/test_m6_system_operations_api_postgres.py src/core/admin-operations src/components/admin/operations src/app/admin tests/m6-admin-operations.test.tsx AGENTS.md
git commit -m "feat: add system operations console"
```

Expected: PASS；平台视图可运营但不获得用户私有数据读取能力。

### Task 16: 建立 pg_dump custom + chunked AEAD backup archive

**Files:**

- Create: `backend/app/recovery/{__init__,archive}.py`
- Create: `backend/scripts/backup_postgres.py`
- Modify: `backend/Makefile`
- Test: `backend/tests/test_m6_backup_archive.py`
- Test: `backend/tests/blocking_io/test_m6_backup_subprocess.py`

**Interfaces:** `BackupArchiveWriter` 将 `pg_dump --format=custom --no-owner --no-acl` 输出按固定 chunk 分块；每块 AES-GCM nonce 唯一，AAD固定含 archive id/schema revision/chunk index；manifest签名并记录 DB high-watermark 与 tombstone journal sequence，不把 key 写入 archive。

- [ ] **Step 1: 写 crypto/subprocess RED tests**

```python
def test_tampered_chunk_fails_before_plaintext_release(tmp_path, backup_key):
    archive = make_archive(tmp_path, backup_key, b"database bytes")
    flip_ciphertext_bit(archive, chunk=1)
    with pytest.raises(BackupAuthenticationFailed):
        list(BackupArchiveReader(backup_key).verified_chunks(archive))

def test_each_chunk_uses_unique_nonce(tmp_path, backup_key):
    manifest = make_archive(tmp_path, backup_key, b"x" * CHUNK_SIZE * 3).manifest
    assert len({chunk.nonce for chunk in manifest.chunks}) == 3
```

覆盖缺 key fail closed、key separation、权限 0600、无 shell插值、pg_dump非零退出不发布 final archive、manifest篡改、stdout/stderr脱敏、blocking I/O不阻塞 event loop。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_backup_archive.py tests/blocking_io/test_m6_backup_subprocess.py -q
```

Expected: FAIL with missing recovery archive module。

- [ ] **Step 3: 实现 authenticated streaming archive**

```python
async def create_backup(config: BackupConfig) -> BackupManifest:
    process = await asyncio.create_subprocess_exec(*pg_dump_argv(config), stdout=PIPE, stderr=PIPE)
    async with BackupArchiveWriter.atomic(config.output, config.key) as writer:
        while chunk := await process.stdout.read(config.chunk_bytes):
            await asyncio.to_thread(writer.write_chunk, chunk)
        if await process.wait() != 0:
            raise BackupCommandFailed("BACKUP_COMMAND_FAILED")
        return await asyncio.to_thread(writer.finalize, await read_high_watermarks(config.database_url))
```

仅在全部 chunk 和 manifest认证完成后原子 rename；CLI输出 archive id/revision/count/digest，不输出 locator secret或数据库 URL。

- [ ] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_backup_archive.py tests/blocking_io/test_m6_backup_subprocess.py -q
git add app/recovery scripts/backup_postgres.py Makefile tests/test_m6_backup_archive.py tests/blocking_io/test_m6_backup_subprocess.py AGENTS.md
git commit -m "feat: add encrypted postgres backups"
```

Expected: PASS；篡改 archive 无法产生可恢复明文。

### Task 17: 建立 external tombstone journal、retention purge、restore 和 drill

**Files:**

- Create: `backend/app/recovery/{journal,purge,restore}.py`
- Create: `backend/scripts/{restore_postgres,drill_restore}.py`
- Modify: `backend/app/private_work/retention.py`
- Test: `backend/tests/test_m6_tombstone_journal.py`
- Test: `backend/tests/test_m6_restore_postgres.py`
- Test: `backend/tests/test_m6_retention_purge_postgres.py`

**Interfaces:** `TombstoneJournal.append()` 向 operator-owned外部文件写 AEAD record + previous digest hash chain并 fsync 后，业务事务才可完成 physical purge；restore只允许新空 `deerflow_restore_*` DB，先认证全 archive，再 `pg_restore`，再从 high-watermark+1 无间断重放 journal，最后执行 M1–M6 probes并写 `restore_proofs`。

- [ ] **Step 1: 写 destructive-path RED tests**

```python
async def test_purge_aborts_when_journal_fsync_fails(purger, expired_project, monkeypatch):
    monkeypatch.setattr(purger.journal, "fsync", Mock(side_effect=OSError("disk full")))
    with pytest.raises(TombstoneJournalUnavailable):
        await purger.purge(expired_project)
    assert await project_private_rows_exist(expired_project.id)

async def test_restore_rejects_sequence_gap(restorer, archive, journal_with_gap):
    with pytest.raises(TombstoneSequenceGap):
        await restorer.restore(archive, journal_with_gap, empty_restore_url())
```

覆盖非空 DB拒绝、源/目标 DB identity相同拒绝、wrong key/tamper、30天窗、project/account/file deletion scope、幂等 replay、probe失败不开服、drill清理仅限随机测试库。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_tombstone_journal.py tests/test_m6_restore_postgres.py tests/test_m6_retention_purge_postgres.py -q
```

Expected: FAIL with missing journal/restore contracts。

- [ ] **Step 3: 实现 journal-first purge 与 new-DB restore**

```python
async def purge(self, candidate: RetentionCandidate) -> None:
    record = await self._build_tombstone(candidate)
    await asyncio.to_thread(self._journal.append_and_fsync, record)
    async with self._sessions.begin() as session:
        await self._repo.verify_still_eligible(session, candidate)
        await self._repo.physically_purge(session, candidate)
        await self._audit.retention_purged(session, candidate.public_ref)
```

restore不覆盖既有 DB，不自动切换 `DATABASE_URL`；operator验证 proof 后另行切流。

- [ ] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_tombstone_journal.py tests/test_m6_restore_postgres.py tests/test_m6_retention_purge_postgres.py -q
git add app/recovery app/private_work/retention.py scripts/restore_postgres.py scripts/drill_restore.py tests/test_m6_tombstone_journal.py tests/test_m6_restore_postgres.py tests/test_m6_retention_purge_postgres.py AGENTS.md
git commit -m "feat: add verifiable recovery workflow"
```

Expected: PASS；journal gap或认证失败时 restore fail closed。

### Task 18: 建立 M6 staged migration、readiness 和多进程运维入口

**Files:**

- Create: `backend/scripts/migrate_reliability.py`
- Create: `backend/scripts/reconcile_usage.py`
- Modify: `backend/scripts/{check_postgres,setup_postgres}.py`
- Modify: `scripts/{doctor.py,serve.sh}`
- Modify: `Makefile`
- Modify: `backend/Makefile`
- Modify: `docker/docker-compose.yaml`
- Modify: `docker/docker-compose-dev.yaml`
- Test: `backend/tests/test_m6_reliability_migration_postgres.py`
- Test: `backend/tests/test_m6_process_readiness.py`
- Test: `tests/test_m6_makefile_contract.py`

**Interfaces:** `migrate-reliability --dry-run` 产生脱敏 inventory且不写；execute要求 maintenance acknowledgment、备份 proof、M4/M5 markers、零 active local execution、quota/stream/job/recovery probes，先到0014、backfill/reconcile、probe后到0015并写 singleton marker。Quota backfill不得只写 aggregate adjustment：它必须为每个 active membership、非零 ready file 和待终态 Run 写入与在线 source key 完全一致的 synthetic exact reservation，再使 counter 收敛；这样迁移前资源首次 remove/delete/terminal 也能精确 release。`make dev/start/up` 启动 Gateway + Worker + 可选 Scheduler；readiness分别暴露 role/worker fleet/scheduler ownership/cutover，不泄露 PID/lock secret。

- [ ] **Step 1: 写 migration/orchestration RED tests**

```python
async def test_dry_run_never_mutates_database(migration_db):
    before = await catalog_digest(migration_db)
    report = await run_cli("migrate-reliability", "--dry-run", database_url=migration_db)
    assert report.exit_code == 0
    assert await catalog_digest(migration_db) == before

async def test_marker_written_only_after_all_probes(pass_until_recovery_probe):
    result = await pass_until_recovery_probe.execute()
    assert result.code == "RECOVERY_PROBE_FAILED"
    assert not await m6_cutover_complete()
```

覆盖 retry/resume ledger、fail-before-DDL finalize、Gateway/Worker/Scheduler role命令、worker缺失 readiness false、scheduler disabled合法、Docker health/dependency、configuration redaction。

同文件必须覆盖迁移前 member/file/Run 的逐资源 synthetic reservation，并在 cutover 后真实执行 remove/delete/terminal，断言 exact release 成功且 counter/ledger 不重复；只有 aggregate reconcile row 时测试必须失败。

- [ ] **Step 2: 观察 RED**

```bash
cd backend
uv run pytest tests/test_m6_reliability_migration_postgres.py tests/test_m6_process_readiness.py -q
cd ..
uv run --project backend pytest tests/test_m6_makefile_contract.py -q
```

Expected: FAIL with missing CLI/targets/process contracts。

- [ ] **Step 3: 实现 staged command 与 orchestration**

```make
migrate-reliability:
	cd backend && uv run python scripts/migrate_reliability.py $(ARGS)

worker:
	cd backend && uv run python -m app.worker.app

scheduler:
	cd backend && uv run python -m app.scheduler.app
```

scripts用独立 PID/log/trap管理三个 backend role；任一 required role启动失败时 root command失败并清理本次创建的进程。

- [ ] **Step 4: 验证并提交**

```bash
cd backend
uv run pytest tests/test_m6_reliability_migration_postgres.py tests/test_m6_process_readiness.py -q
cd ..
uv run --project backend pytest tests/test_m6_makefile_contract.py -q
git add backend/scripts backend/Makefile backend/tests/test_m6_reliability_migration_postgres.py backend/tests/test_m6_process_readiness.py scripts Makefile docker tests/test_m6_makefile_contract.py AGENTS.md backend/AGENTS.md
git commit -m "feat: operationalize M6 cutover"
```

Expected: PASS；marker完成前所有 M6 project/platform endpoints保持关闭。

### Task 19: 建立真实 PostgreSQL、多进程、Frontend 和 recovery release gates

**Files:**

- Create: `backend/tests/test_m6_release_gate_postgres.py`
- Create: `backend/tests/test_m6_worker_crash_recovery_postgres.py`
- Create: `backend/tests/test_m6_gateway_reconnect_process.py`
- Create: `frontend/tests/m6-static-gates.test.tsx`
- Modify: `.github/workflows/project-foundation-postgres-tests.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile`

**Interfaces:** PostgreSQL release job在 pytest 前硬校验 `POSTGRES_TEST_URL`，运行 M1–M6 真实文件且 0 skip；多进程 tests实际启动两个 Worker/两个 Gateway验证 lease takeover和 SSE replay；recovery gate实际创建加密 archive、恢复随机新库、重放 journal并跑 schema/isolation probes。

- [ ] **Step 1: 先写会暴露缺口的 release tests**

```python
async def test_worker_sigkill_is_taken_over_without_duplicate_terminal(worker_cluster, admitted_run):
    first = await worker_cluster.wait_until_leased(admitted_run.job_id)
    await worker_cluster.sigkill(first.worker_id)
    await worker_cluster.advance_past_lease()
    await worker_cluster.wait_terminal(admitted_run.run_id)
    assert await terminal_frame_count(admitted_run.run_id) == 1
    assert await successful_attempt_count(admitted_run.job_id) == 1
```

覆盖 Gateway restart cursor、scheduler takeover、unsafe dead、不越 scope、quota race、audit redaction、backup tamper/gap、static gates。

- [ ] **Step 2: 运行 targeted release RED 并修正实现缺口**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m6_release_gate_postgres.py tests/test_m6_worker_crash_recovery_postgres.py tests/test_m6_gateway_reconnect_process.py -q
cd ../frontend
pnpm test -- m6-static-gates.test.tsx
```

Expected: 首次运行至少一个新增 end-to-end invariant失败；只修实现，不削弱断言或用 skip规避。

- [ ] **Step 3: 接入 CI 和 root gate**

CI先执行 `test -n "$POSTGRES_TEST_URL"`，再运行既有八个 M1–M5 文件和新增 M6 schema/job/stream/quota/audit/recovery/release files；root `make test`/专用 `make test-project-foundation-postgres` 输出 collected/passed/skipped统计。

- [ ] **Step 4: fresh 验证并提交**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m6_release_gate_postgres.py tests/test_m6_worker_crash_recovery_postgres.py tests/test_m6_gateway_reconnect_process.py -q
cd ../frontend
pnpm test -- m6-static-gates.test.tsx
pnpm check
cd ..
git diff --check
git add backend/tests/test_m6_release_gate_postgres.py backend/tests/test_m6_worker_crash_recovery_postgres.py backend/tests/test_m6_gateway_reconnect_process.py frontend/tests/m6-static-gates.test.tsx .github/workflows Makefile
git commit -m "test: gate M6 reliability release"
```

Expected: PASS with 0 skip in M6 PostgreSQL evidence。

### Task 20: 同步文档、运行全量门禁并完成独立关闭审查

**Files:**

- Create: `docs/operations/{m6-reliability-migration,m6-backup-recovery}.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `backend/AGENTS.md`
- Modify: `frontend/AGENTS.md`
- Modify: `docs/superpowers/specs/2026-07-12-project-first-saas-design.md`
- Modify: `docs/superpowers/specs/2026-07-16-project-reliability-governance-m6-design.md`

**Interfaces:** runbooks给出 dry-run→maintenance→backup proof→execute→check-db→M1–M6 probes→separate restore drill 的精确顺序、失败决策与不可 downgrade规则；状态文档只更新为 6/8（75%），明确 M7/M8未完成。

- [ ] **Step 1: 写文档 contract checks**

```bash
rg -n 'dry-run|maintenance|backup proof|0014|0015|check-db|restore drill|no downgrade' docs/operations/m6-*.md
rg -n '6/8|75%|M7|M8' AGENTS.md docs/superpowers/specs/2026-07-12-project-first-saas-design.md
```

Expected: 在文档创建前命令失败或缺少 required terms。

- [ ] **Step 2: 完成 operator/user/developer 文档并运行 fresh full gates**

```bash
cd backend
uv run ruff format --check .
uv run ruff check .
uv run pytest -q
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m6_reliability_schema_postgres.py tests/test_m6_reliability_migration_postgres.py tests/test_m6_release_gate_postgres.py -q
cd ../frontend
pnpm check
pnpm test
pnpm build
cd ..
make doctor
make check-db
git diff --check
```

Expected: 所有命令 exit 0；PostgreSQL release evidence 0 skip；build不暴露未授权 governance入口。

- [ ] **Step 3: 执行独立关闭审查并只修实质问题**

使用 `superpowers:requesting-code-review` 检查规格覆盖、scope/lease authority、retry safety、SSE顺序、quota原子性、audit privacy、backup authentication、journal连续性、migration fail-before-DDL和文档状态；任何 P0/P1/P2 先写复现测试再修复，并重新运行受影响 gate和 full gate。

- [ ] **Step 4: 最终提交**

```bash
git add README.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md docs/operations docs/superpowers/specs/2026-07-12-project-first-saas-design.md docs/superpowers/specs/2026-07-16-project-reliability-governance-m6-design.md
git commit -m "docs: complete M6 reliability milestone"
git status --short
```

Expected: working tree clean；M6证据和文档一致，但不宣称 M7/M8 已完成。

## File Structure

### Persistence and schema

- `backend/packages/harness/deerflow/persistence/jobs/{model,sql}.py` — job、attempt、dead projection、Worker node ORM 与 session-bound repository。
- `backend/packages/harness/deerflow/persistence/quotas/{model,sql}.py` — quota policy、counter、append-only ledger 和 reconciliation repository。
- `backend/packages/harness/deerflow/persistence/audit/{model,sql}.py` — append-only audit ORM 与 project/system scoped reader/writer。
- `backend/packages/harness/deerflow/persistence/recovery/model.py` — deletion tombstone 与 restore proof metadata。
- `backend/packages/harness/deerflow/persistence/reliability/model.py` — M6 migration run/ledger/cutover singleton。
- `backend/packages/harness/deerflow/persistence/migrations/versions/0014_project_reliability_expand.py` — M6 control tables、nullable Run/job/occurrence relations、indexes/triggers。
- `backend/packages/harness/deerflow/persistence/migrations/versions/0015_project_reliability_finalize.py` — probe-first final constraints 和 marker-ready schema。
- `backend/packages/harness/deerflow/persistence/run/model.py`、`scheduled_task_runs/model.py` — execution job/lease relation。

### Backend domains

- `backend/app/reliability/{models,errors,error_mapping,cutover,readiness}.py` — immutable contracts、stable errors、M4+M5+M6 gate。
- `backend/app/reliability/{jobs,workers,execution}.py` — enqueue/claim/heartbeat/settlement、Worker fleet 和 private execution handler。
- `backend/app/quotas/{models,service,reconciliation}.py` — effective limits、atomic reserve/release/consume。
- `backend/app/audit/{models,service,sinks}.py` — whitelist、HMAC target refs、project/platform audit ports。
- `backend/app/recovery/{archive,journal,purge,restore}.py` — chunked AEAD archive、hash-chained journal、purge 和 restore orchestration。
- `backend/app/worker/{app,service}.py` — 独立 Worker entrypoint/lifecycle；不得 import Gateway router。
- `backend/app/scheduler/app.py`、`backend/app/scheduler/service.py` — 独立 Scheduler entrypoint，复用 M5 ownership。

### Gateway and integration

- `backend/app/gateway/services.py` — private admission/enqueue，不再 launch local task。
- `backend/app/private_work/{run_admission,run_repository,run_service,retention}.py` — session-bound admission、execution authority、quota/audit hooks。
- `backend/app/automations/{dispatcher,occurrences,reconciliation}.py` — occurrence + Run + job transaction 与 Worker settlement。
- `backend/app/gateway/routers/{private_work,project_governance,project_members,project_usage,project_audit,admin_operations,admin_projects,admin_jobs,admin_audit}.py` — strict scoped HTTP。
- `backend/packages/harness/deerflow/runtime/stream_bridge/postgres.py` — PostgreSQL stream writer/reader。
- `backend/packages/harness/deerflow/runtime/events/store/{base,db}.py` — stream page/terminal operations。
- `backend/app/gateway/deps.py`、`app.py` — Gateway-only dependencies 和 readiness，不启动 Worker/Scheduler。

### Operations

- `backend/scripts/{migrate_reliability,backup_postgres,restore_postgres,reconcile_usage,drill_restore}.py` — trusted CLI。
- `backend/scripts/{check_postgres,setup_postgres}.py`、`scripts/{doctor,serve}.py|.sh` — head/tables/process/readiness。
- `Makefile`、`backend/Makefile`、`docker/*.yaml` — Gateway/Worker/Scheduler orchestration。
- `docs/operations/{m6-reliability-migration,m6-backup-recovery}.md` — maintenance/cutover/recovery runbook。

### Frontend

- `frontend/src/core/project-governance/{usage,audit}.ts` — strict project API/contracts/query keys。
- `frontend/src/core/admin-operations/{types,api,query-keys}.ts` — strict system admin operations contracts。
- `frontend/src/components/projects/governance/{project-usage-page,project-audit-page}.tsx` — Admin-only project UI。
- `frontend/src/components/admin/operations/{admin-operations-shell,operations-overview,admin-projects,admin-jobs,admin-audit}.tsx` — platform UI。
- `frontend/src/app/projects/[project_slug]/settings/{usage,audit}/page.tsx`、`frontend/src/app/admin/{operations,projects,jobs,audit}/page.tsx` — route wrappers。
- `frontend/src/core/private-work/{client,registry}.ts`、chat hooks — durable reconnect cursor and terminal dedupe。

## Gate and Dependency Order

1. **Gate 1 — final schema and reliable queue:** Tasks 1–4。
2. **Gate 2 — Run/Automation execution and durable stream:** Tasks 5–9。
3. **Gate 3 — quota and audit governance:** Tasks 10–15。
4. **Gate 4 — recovery, cutover, release evidence:** Tasks 16–20。

M6 cutover marker只能在 Task 18 migration/recovery probes全通过后写入；项目/平台入口只能在 Tasks 14–15 的 capability、readiness、cache isolation和静态门禁通过后开放；M6完成状态只能在 Task 20 fresh full gate与独立关闭审查后更新。

---
