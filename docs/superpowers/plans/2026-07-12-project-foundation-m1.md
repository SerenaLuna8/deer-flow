# M1 项目基础实施计划

> **面向执行代理：** REQUIRED SUB-SKILL: 使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐项执行本计划。所有步骤使用复选框跟踪。

**目标：** 将 DeerFlow 的全部现有 SQLite 持久化数据原样迁移至 PostgreSQL，永久移除 SQLite 运行后端，并交付项目、成员关系、`ProjectContext`、项目 API、项目工作台和项目主页的基础闭环。

**架构：** 先建立只读 SQLite 清点和幂等迁移工具，在维护窗口内完成 PostgreSQL 切换；随后在 `app.projects` 中实现项目业务域，在 `deerflow.persistence.projects` 中实现数据库模型。前端使用独立 `core/projects` 领域和账户/项目作用域查询键，M4 完成前通过 `project_private_workspace` 功能门禁禁止从项目页面创建 Thread。

**技术栈：** Python 3.12+、FastAPI、Pydantic、SQLAlchemy Async、Alembic、asyncpg、PostgreSQL、LangGraph PostgreSQL checkpointer、Next.js 16、React 19、TypeScript、TanStack Query、Rstest、Playwright。

## 全局约束

- 所有里程碑当前完成度为 0；计划执行不得复用未合并的旧项目实现作为完成证据。
- PostgreSQL 是唯一运行数据库；SQLite 只允许被一次性迁移脚本以只读方式访问。
- 不使用 PostgreSQL RLS；隔离依赖业务服务、作用域仓储和数据库约束。
- 本地数据库通过 Docker 暴露在 `127.0.0.1:5432`，目标数据库名固定为 `deerflow`。
- 数据库密码只从 `DATABASE_URL` 读取，不进入代码、配置样例、日志、测试输出或提交。
- 所有 schema 变更只通过 Alembic；运行时不使用 `Base.metadata.create_all()` 建表。
- `harness -> app` 导入禁令保持不变。
- 后端功能和修复执行 TDD；每个任务先见到目标测试失败，再写最小实现。
- 所有后端持久化测试使用真实 PostgreSQL，不使用 SQLite 替代。
- M1 不得作为完整多用户 SaaS 发布；`project_private_workspace` 默认关闭。
- 不修改或提交工作区中与本计划无关的现有改动。

---

## 文件与模块结构

### PostgreSQL 切换与迁移

- `backend/scripts/sqlite_inventory.py`：只读扫描 SQLite 来源并生成 JSON 清单。
- `backend/scripts/migrate_sqlite_to_postgres.py`：幂等复制、校验和迁移报告。
- `backend/scripts/setup_postgres.py`：创建目标数据库、运行 Alembic、调用初始化服务。
- `backend/scripts/check_postgres.py`：只读检查连接、revision、表和约束。
- `backend/tests/postgres_utils.py`：测试数据库生命周期工具。
- `backend/tests/conftest.py`：`postgres_database_url` 和已迁移数据库夹具。
- `backend/packages/harness/deerflow/config/database_config.py`：只接受 PostgreSQL URL。
- `backend/packages/harness/deerflow/persistence/engine.py`：只创建 asyncpg engine。
- `backend/packages/harness/deerflow/runtime/checkpointer/`：只创建 PostgreSQL saver/store。

### 项目后端

- `backend/packages/harness/deerflow/persistence/migration_ledger/model.py`：一次性迁移的幂等台账。
- `backend/packages/harness/deerflow/persistence/projects/model.py`：`ProjectRow` 和 `ProjectMembershipRow`。
- `backend/app/projects/capabilities.py`：角色到能力的唯一映射。
- `backend/app/projects/context.py`：不可变 `ProjectContext` 和解析器。
- `backend/app/projects/errors.py`：稳定领域错误。
- `backend/app/projects/models.py`：领域输入、输出和游标值对象。
- `backend/app/projects/repository.py`：作用域仓储协议和 SQLAlchemy 实现。
- `backend/app/projects/service.py`：事务、授权和项目不变量。
- `backend/app/gateway/routers/projects.py`：`/api/projects` API。

### 项目前端

- `frontend/src/core/projects/types.ts`：Zod schema 和 TypeScript 类型。
- `frontend/src/core/projects/api.ts`：项目 API 客户端。
- `frontend/src/core/projects/query-keys.ts`：账户和项目作用域查询键。
- `frontend/src/core/projects/hooks.ts`：TanStack Query hooks 和 mutations。
- `frontend/src/app/workspace/projects/page.tsx`：项目工作台。
- `frontend/src/app/projects/[project_slug]/page.tsx`：项目主页。
- `frontend/src/components/projects/`：项目卡片、创建表单、空状态和项目页头。

---

## 第一交付段：PostgreSQL 迁移与切换

### 任务 1：只读 SQLite 清点器

**文件：**

- 新建：`backend/scripts/sqlite_inventory.py`
- 新建：`backend/tests/test_sqlite_inventory.py`

**接口：**

- 输入：显式 SQLite 文件路径列表。
- 输出：`SQLiteInventory(path, sha256, size_bytes, integrity, tables)`。
- 后续依赖：任务 5 的迁移器复用 `inspect_sqlite()` 和 `table_digest()`。

- [ ] **步骤 1：编写只读和清单失败测试**

```python
def test_inspect_sqlite_opens_source_read_only(tmp_path):
    source = seed_sqlite(tmp_path / "legacy.db", {"users": [("u1", "a@example.com")]})
    before = source.stat().st_mtime_ns
    inventory = inspect_sqlite(source)
    assert inventory.integrity == "ok"
    assert inventory.tables[0].name == "users"
    assert inventory.tables[0].row_count == 1
    assert source.stat().st_mtime_ns == before


def test_inspect_sqlite_rejects_corrupt_source(tmp_path):
    source = tmp_path / "broken.db"
    source.write_bytes(b"not sqlite")
    with pytest.raises(InventoryError, match="integrity"):
        inspect_sqlite(source)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`cd backend && uv run pytest tests/test_sqlite_inventory.py -q`

预期：测试收集失败，提示 `scripts.sqlite_inventory` 或 `inspect_sqlite` 不存在。

- [ ] **步骤 3：实现只读扫描和稳定摘要**

```python
@dataclass(frozen=True)
class TableInventory:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    row_count: int
    digest: str


@dataclass(frozen=True)
class SQLiteInventory:
    path: str
    sha256: str
    size_bytes: int
    integrity: str
    tables: tuple[TableInventory, ...]


def open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def inspect_sqlite(path: Path) -> SQLiteInventory:
    if not path.is_file():
        raise InventoryError(f"SQLite source not found: {path}")
    with open_read_only(path) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise InventoryError(f"SQLite integrity check failed: {integrity}")
        tables = tuple(inspect_table(connection, name) for name in list_user_tables(connection))
    return SQLiteInventory(
        path=str(path.resolve()),
        sha256=file_sha256(path),
        size_bytes=path.stat().st_size,
        integrity=integrity,
        tables=tables,
    )
```

摘要按主键顺序序列化每一行；没有主键的表使用全部列排序。摘要输入包含列名、类型标签和 `NULL` 标记，避免字符串拼接碰撞。

- [ ] **步骤 4：运行清点器测试**

运行：`cd backend && uv run pytest tests/test_sqlite_inventory.py -q`

预期：全部通过。

- [ ] **步骤 5：提交**

```bash
git add backend/scripts/sqlite_inventory.py backend/tests/test_sqlite_inventory.py
git commit -m "feat(database): inventory legacy SQLite sources"
```

### 任务 2：PostgreSQL-only 依赖与配置契约

**文件：**

- 修改：`backend/packages/harness/pyproject.toml`
- 修改：`backend/pyproject.toml`
- 修改：`backend/packages/harness/deerflow/config/database_config.py`
- 修改：`backend/packages/harness/deerflow/config/app_config.py`
- 删除：`backend/packages/harness/deerflow/config/checkpointer_config.py`
- 修改：`backend/packages/harness/deerflow/config/reload_boundary.py`
- 修改：`config.example.yaml`
- 修改：`scripts/detect_uv_extras.py`
- 测试：`backend/tests/test_persistence_scaffold.py`
- 测试：`backend/tests/test_checkpointer.py`
- 测试：`backend/tests/test_detect_uv_extras.py`

**接口：**

- 产生：`DatabaseConfig(url: str, pool_size: int, max_overflow: int, pool_timeout_seconds: int, statement_timeout_seconds: int)`。
- 约束：`url` 必须使用 `postgresql://` 或 `postgresql+asyncpg://`。
- 后续依赖：所有 engine、migration、checkpointer 和脚本只读取 `config.database.url`。

- [ ] **步骤 1：把配置测试改为拒绝非 PostgreSQL URL**

```python
def test_database_config_requires_postgres_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        DatabaseConfig()


@pytest.mark.parametrize("url", ["sqlite:///tmp/x.db", "memory://"])
def test_database_config_rejects_non_postgres(url):
    with pytest.raises(ValidationError, match="PostgreSQL"):
        DatabaseConfig(url=url)


def test_database_config_normalizes_asyncpg_url():
    config = DatabaseConfig(url="postgresql://u:p@127.0.0.1:5432/deerflow")
    assert config.sqlalchemy_url == "postgresql+asyncpg://u:p@127.0.0.1:5432/deerflow"
    assert config.checkpointer_url == "postgresql://u:p@127.0.0.1:5432/deerflow"
```

- [ ] **步骤 2：运行配置测试确认旧多后端模型失败**

运行：`cd backend && uv run pytest tests/test_persistence_scaffold.py tests/test_checkpointer.py tests/test_detect_uv_extras.py -q`

预期：新断言失败，因为现有配置仍接受 `memory` 和 `sqlite`。

- [ ] **步骤 3：收敛依赖和配置模型**

```python
class DatabaseConfig(BaseModel):
    url: str = Field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    pool_timeout_seconds: int = Field(default=30, ge=1)
    statement_timeout_seconds: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def validate_postgres(self) -> "DatabaseConfig":
        if not self.url:
            raise ValueError("DATABASE_URL is required")
        if not self.url.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return self

    @property
    def sqlalchemy_url(self) -> str:
        return self.url.replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def checkpointer_url(self) -> str:
        return self.url.replace("postgresql+asyncpg://", "postgresql://", 1)
```

将 `asyncpg`、`psycopg[binary,pool]`、`langgraph-checkpoint-postgres` 从 optional dependency 移到默认依赖；删除 `aiosqlite` 和 `langgraph-checkpoint-sqlite`。`scripts/detect_uv_extras.py` 不再根据数据库配置添加 `postgres` extra，只保留其他可选能力检测。

- [ ] **步骤 4：更新配置样例**

```yaml
database:
  url: $DATABASE_URL
  pool_size: 5
  max_overflow: 10
  pool_timeout_seconds: 30
  statement_timeout_seconds: 30
```

删除 `database.backend`、`sqlite_dir`、独立 `checkpointer.type` 和 SQLite/PostgreSQL 选择说明。

- [ ] **步骤 5：运行配置和依赖测试**

运行：`cd backend && uv run pytest tests/test_persistence_scaffold.py tests/test_checkpointer.py tests/test_detect_uv_extras.py -q`

预期：全部通过，测试文件不再包含 SQLite backend 成功路径。

- [ ] **步骤 6：提交**

```bash
git add backend/packages/harness/pyproject.toml backend/pyproject.toml backend/packages/harness/deerflow/config/database_config.py backend/packages/harness/deerflow/config/app_config.py backend/packages/harness/deerflow/config/checkpointer_config.py backend/packages/harness/deerflow/config/reload_boundary.py config.example.yaml scripts/detect_uv_extras.py backend/tests/test_persistence_scaffold.py backend/tests/test_checkpointer.py backend/tests/test_detect_uv_extras.py
git commit -m "refactor(database): require PostgreSQL configuration"
```

### 任务 3：PostgreSQL engine、checkpointer 和测试基础

**文件：**

- 修改：`backend/packages/harness/deerflow/persistence/engine.py`
- 修改：`backend/packages/harness/deerflow/persistence/bootstrap.py`
- 修改：`backend/packages/harness/deerflow/persistence/migrations/env.py`
- 修改：`backend/packages/harness/deerflow/runtime/checkpointer/provider.py`
- 修改：`backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py`
- 重命名：`backend/app/gateway/auth/repositories/sqlite.py` → `backend/app/gateway/auth/repositories/sql.py`
- 修改：`backend/app/gateway/auth/reset_admin.py`
- 新建：`backend/tests/postgres_utils.py`
- 新建：`backend/tests/test_postgres_fixture.py`
- 修改：`backend/tests/conftest.py`
- 删除：`backend/tests/blocking_io/test_persistence_engine_sqlite.py`
- 删除：`backend/tests/test_persistence_bootstrap_sqlite_lock.py`
- 修改：任务 2 扫描列出的所有 SQLite 持久化测试文件。

**接口：**

- 产生：`init_engine(config: DatabaseConfig) -> AsyncEngine`。
- 产生：`postgres_database_url` pytest fixture，每个测试获得独立临时数据库。
- 产生：`SQLUserRepository`，替代名称误导的 `SQLiteUserRepository`。

- [ ] **步骤 1：新增 PostgreSQL 测试数据库生命周期测试**

```python
@pytest.mark.asyncio
async def test_postgres_database_fixture_is_isolated(postgres_database_url):
    engine = create_async_engine(postgres_database_url)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE probe (id integer primary key)"))
        await connection.execute(text("INSERT INTO probe VALUES (1)"))
        assert await connection.scalar(text("SELECT count(*) FROM probe")) == 1
    await engine.dispose()
```

- [ ] **步骤 2：运行测试确认 fixture 不存在**

运行：`cd backend && uv run pytest tests/test_postgres_fixture.py -q`

预期：fixture lookup 失败。

- [ ] **步骤 3：实现临时 PostgreSQL 数据库 fixture**

```python
@asynccontextmanager
async def temporary_postgres_database(admin_url: str):
    name = f"deerflow_test_{os.getpid()}_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(admin_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        yield replace_database(admin_url, name)
    finally:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()
```

数据库名完全由进程号和 UUID 生成，不接收用户输入。fixture 从 `POSTGRES_TEST_URL` 读取维护连接；缺失时明确 skip PostgreSQL integration 标记，而 CI 和 M1 门禁必须设置该变量。

- [ ] **步骤 4：移除 engine 和 bootstrap 的 SQLite 分支**

```python
async def init_engine(config: DatabaseConfig) -> AsyncEngine:
    engine = create_async_engine(
        config.sqlalchemy_url,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_timeout=config.pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={"server_settings": {"statement_timeout": f"{config.statement_timeout_seconds}s"}},
    )
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return engine
```

删除 `_SQLITE_LOCKS`、SQLite WAL、busy timeout、memory no-op 和 dialect 分支。bootstrap 只使用 PostgreSQL advisory lock。

- [ ] **步骤 5：统一 checkpointer 和 User repository**

checkpointer 与 store 始终通过 `config.database.checkpointer_url` 创建 PostgreSQL pool。将 `SQLiteUserRepository` 重命名为 `SQLUserRepository`，更新所有 import、测试和文档字符串。

- [ ] **步骤 6：迁移测试到 PostgreSQL fixture**

逐个修改以下测试，使 repository/engine fixture 使用 `postgres_database_url`：

```text
backend/tests/test_additional_channel_connections.py
backend/tests/test_auth.py
backend/tests/test_channel_connections_repository.py
backend/tests/test_channel_connections_router.py
backend/tests/test_channels.py
backend/tests/test_console_router.py
backend/tests/test_discord_channel_connections.py
backend/tests/test_feedback.py
backend/tests/test_initialize_admin.py
backend/tests/test_owner_isolation.py
backend/tests/test_persistence_bootstrap.py
backend/tests/test_persistence_bootstrap_concurrency.py
backend/tests/test_persistence_bootstrap_regression.py
backend/tests/test_persistence_bootstrap_url.py
backend/tests/test_persistence_timezone.py
backend/tests/test_run_event_store.py
backend/tests/test_run_event_store_filter.py
backend/tests/test_run_repository.py
backend/tests/test_scheduled_task_claims.py
backend/tests/test_scheduled_task_repository.py
backend/tests/test_slack_channel_connections.py
backend/tests/test_telegram_channel_connections.py
backend/tests/test_thread_meta_repo.py
backend/tests/test_token_usage_by_model.py
backend/tests/test_wechat_channel.py
backend/tests/blocking_io/test_persistence_bootstrap.py
```

纯 SQL 编译测试可以继续使用 `postgresql.dialect()`，不需要连接数据库。SQLite 专项锁和 event-loop 文件 IO 测试删除或改写为 PostgreSQL pool 行为测试。

- [ ] **步骤 7：运行持久化测试组**

运行：

```bash
cd backend
POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest \
  tests/test_persistence_scaffold.py \
  tests/test_persistence_bootstrap.py \
  tests/test_checkpointer.py \
  tests/test_auth.py \
  tests/test_thread_meta_repo.py \
  tests/test_run_repository.py \
  tests/test_run_event_store.py \
  tests/test_scheduled_task_repository.py -q
```

预期：全部通过，`rg -n "sqlite+aiosqlite|init_engine\(\"sqlite\"" backend/tests` 无匹配。

- [ ] **步骤 8：提交**

```bash
git add backend/packages/harness/deerflow/persistence backend/packages/harness/deerflow/runtime/checkpointer backend/app/gateway/auth backend/tests
git commit -m "refactor(database): run persistence on PostgreSQL only"
```

### 任务 4：数据库 setup、check 和 Makefile 入口

**文件：**

- 新建：`backend/scripts/setup_postgres.py`
- 新建：`backend/scripts/check_postgres.py`
- 新建：`backend/tests/test_setup_postgres.py`
- 新建：`backend/tests/test_check_postgres.py`
- 修改：`backend/Makefile`
- 修改：`Makefile`

**接口：**

- `ensure_database(admin_url: str, database_name: str = "deerflow") -> bool`，返回是否新建。
- `check_postgres(database_url: str) -> PostgresCheckResult`。
- CLI 永不打印未脱敏 URL。

- [ ] **步骤 1：编写幂等创建和脱敏测试**

```python
@pytest.mark.asyncio
async def test_ensure_database_is_idempotent(postgres_admin_url):
    assert await ensure_database(postgres_admin_url, "deerflow_test_setup") is True
    assert await ensure_database(postgres_admin_url, "deerflow_test_setup") is False


def test_setup_output_never_contains_password(capsys):
    print_result(PostgresCheckResult(host="127.0.0.1", port=5432, database="deerflow", revision="head"))
    assert "secret" not in capsys.readouterr().out
```

- [ ] **步骤 2：运行确认失败**

运行：`cd backend && uv run pytest tests/test_setup_postgres.py tests/test_check_postgres.py -q`

预期：模块不存在。

- [ ] **步骤 3：实现固定目标数据库创建**

```python
async def ensure_database(admin_url: str, database_name: str = "deerflow") -> bool:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database_name):
        raise ValueError("invalid PostgreSQL database name")
    connection = await asyncpg.connect(admin_url)
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database_name)
        if exists:
            return False
        await connection.execute(f'CREATE DATABASE "{database_name}"')
        return True
    finally:
        await connection.close()
```

`setup_postgres.py` 创建数据库后调用现有 bootstrap/Alembic API；`check_postgres.py` 只读检查 PostgreSQL 版本、当前 revision、head revision 和 M1 必需表。

- [ ] **步骤 4：增加 Makefile 入口**

```make
setup-db:
	cd backend && uv run python scripts/setup_postgres.py

migrate-db:
	cd backend && uv run alembic -c packages/harness/deerflow/persistence/migrations/alembic.ini upgrade head

check-db:
	cd backend && uv run python scripts/check_postgres.py

migrate-sqlite:
	cd backend && uv run python scripts/migrate_sqlite_to_postgres.py
```

- [ ] **步骤 5：运行脚本测试和本地只读检查**

运行：

```bash
cd backend && POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest tests/test_setup_postgres.py tests/test_check_postgres.py -q
make check-db
```

预期：测试通过；`make check-db` 输出 host、port、database 和 revision，不输出密码。

- [ ] **步骤 6：提交**

```bash
git add backend/scripts/setup_postgres.py backend/scripts/check_postgres.py backend/tests/test_setup_postgres.py backend/tests/test_check_postgres.py backend/Makefile Makefile
git commit -m "feat(database): add PostgreSQL setup and health commands"
```

### 任务 5：SQLite 到 PostgreSQL 幂等迁移器

**文件：**

- 新建：`backend/packages/harness/deerflow/persistence/migrations/versions/0004_migration_ledger.py`
- 新建：`backend/packages/harness/deerflow/persistence/migration_ledger/model.py`
- 新建：`backend/packages/harness/deerflow/persistence/migration_ledger/__init__.py`
- 修改：`backend/packages/harness/deerflow/persistence/models/__init__.py`
- 新建：`backend/scripts/migrate_sqlite_to_postgres.py`
- 新建：`backend/tests/test_sqlite_to_postgres_migration.py`

**接口：**

- `migrate_source(source: Path, target_url: str, dry_run: bool) -> MigrationReport`。
- 幂等键：`source_sha256 + table_name + source_primary_key`。
- 迁移器只复制目标 schema 已知表；发现未知来源表时停止。

- [ ] **步骤 1：编写完整复制、重跑和故障回滚测试**

```python
@pytest.mark.asyncio
async def test_migrate_source_copies_and_replays_without_duplicates(legacy_sqlite, postgres_database_url):
    first = await migrate_source(legacy_sqlite, postgres_database_url, dry_run=False)
    second = await migrate_source(legacy_sqlite, postgres_database_url, dry_run=False)
    assert first.tables["users"].inserted == 1
    assert second.tables["users"].inserted == 0
    assert second.tables["users"].already_migrated == 1
    assert second.verified is True


@pytest.mark.asyncio
async def test_migrate_source_rolls_back_failed_table(legacy_sqlite, postgres_database_url, monkeypatch):
    monkeypatch.setattr(MigrationWriter, "write_row", fail_on_second_row)
    with pytest.raises(MigrationError):
        await migrate_source(legacy_sqlite, postgres_database_url, dry_run=False)
    assert await migrated_ledger_count(postgres_database_url) == 0
```

- [ ] **步骤 2：运行确认失败**

运行：`cd backend && POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest tests/test_sqlite_to_postgres_migration.py -q`

预期：迁移器和 ledger model 不存在。

- [ ] **步骤 3：建立 migration ledger**

```python
class MigrationLedgerRow(Base):
    __tablename__ = "migration_ledger"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_table: Mapped[str] = mapped_column(String(128), nullable=False)
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    target_table: Mapped[str] = mapped_column(String(128), nullable=False)
    target_key: Mapped[str] = mapped_column(Text, nullable=False)
    row_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    migrated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("source_sha256", "source_table", "source_key", name="uq_migration_source_row"),
    )
```

- [ ] **步骤 4：实现显式表映射**

迁移器定义固定映射，不根据来源 SQL 动态建表：

```python
TABLE_ORDER = (
    "users",
    "threads_meta",
    "runs",
    "run_events",
    "feedback",
    "scheduled_tasks",
    "scheduled_task_runs",
    "channel_connections",
    "channel_credentials",
    "channel_oauth_states",
    "channel_conversations",
)

LANGGRAPH_TABLE_ORDER = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)
```

LangGraph checkpoint 表使用 LangGraph PostgreSQL saver 的 schema 初始化后，按照 `LANGGRAPH_TABLE_ORDER` 单独映射。每张表由显式 `ColumnRule` 列表处理 Boolean、JSON、UTC 时间和 nullable 值；不存在于两个允许列表的来源表使迁移失败。

- [ ] **步骤 5：实现 dry-run、事务和验证**

每张表在独立 PostgreSQL 事务中复制，ledger 与目标行同事务提交。完成后比较来源和目标主键集合、行数和稳定摘要，并重置所有 identity/sequence。任一校验不一致时 `MigrationReport.verified=False`，CLI 退出码为 1，禁止写入切换完成标记。

- [ ] **步骤 6：运行迁移测试**

运行：`cd backend && POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest tests/test_sqlite_inventory.py tests/test_sqlite_to_postgres_migration.py -q`

预期：全部通过，并覆盖 dry-run、重复执行、损坏来源、未知表、重复主键、JSON 失败、事务回滚和 sequence 重置。

- [ ] **步骤 7：提交**

```bash
git add backend/packages/harness/deerflow/persistence/migrations/versions/0004_migration_ledger.py backend/packages/harness/deerflow/persistence/migration_ledger backend/packages/harness/deerflow/persistence/models/__init__.py backend/scripts/migrate_sqlite_to_postgres.py backend/tests/test_sqlite_to_postgres_migration.py
git commit -m "feat(database): migrate legacy SQLite data to PostgreSQL"
```

---

## 第二交付段：项目后端基础

### 任务 6：平台角色和项目 schema

**文件：**

- 新建：`backend/packages/harness/deerflow/persistence/migrations/versions/0005_project_foundation.py`
- 修改：`backend/packages/harness/deerflow/persistence/user/model.py`
- 扩展：`backend/packages/harness/deerflow/persistence/projects/model.py`
- 新建：`backend/packages/harness/deerflow/persistence/projects/__init__.py`
- 修改：`backend/packages/harness/deerflow/persistence/models/__init__.py`
- 修改：`backend/app/gateway/auth/models.py`
- 修改：`backend/app/gateway/auth/local_provider.py`
- 修改：`backend/app/gateway/routers/auth.py`
- 修改：`backend/app/gateway/app.py`
- 测试：`backend/tests/test_auth_type_system.py`
- 新建：`backend/tests/test_project_schema_postgres.py`

**接口：**

- 产生：`ProjectRow` 和 `ProjectMembershipRow`。
- 平台角色：`Literal["system_admin", "user"]`。
- 项目 role：`admin|editor|runner|viewer`；M1 membership status 仅 `active`。

- [ ] **步骤 1：编写 schema 和角色 migration 测试**

```python
@pytest.mark.asyncio
async def test_admin_role_is_migrated_to_system_admin(migrated_legacy_database):
    assert await scalar(migrated_legacy_database, "SELECT system_role FROM users") == "system_admin"


@pytest.mark.asyncio
async def test_membership_unique_per_project_and_user(project_database):
    await insert_membership(project_database, project_id=P1, user_id=U1, role="admin")
    with pytest.raises(IntegrityError):
        await insert_membership(project_database, project_id=P1, user_id=U1, role="viewer")
```

- [ ] **步骤 2：运行确认失败**

运行：`cd backend && POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest tests/test_auth_type_system.py tests/test_project_schema_postgres.py -q`

预期：角色断言失败且项目表不存在。

- [ ] **步骤 3：实现项目 ORM 模型**

```python
class ProjectRow(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    icon: Mapped[str] = mapped_column(String(32), nullable=False, default="folder")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    membership_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectMembershipRow(Base):
    __tablename__ = "project_memberships"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Alembic 增加 slug 小写/格式、status、role、version CHECK constraints，以及 `(project_id, user_id)` unique constraint。migration 把现有 `users.system_role='admin'` 更新为 `system_admin`，再增加角色 CHECK constraint。

- [ ] **步骤 4：更新所有平台角色调用点**

后端把 `admin` 替换为 `system_admin`，包括初始化、auth-disabled、OIDC provisioning、skill 管理和 artifact 管理测试。项目角色的 `admin` 保持小写，不与平台角色混用。

- [ ] **步骤 5：运行 schema 测试**

运行：`cd backend && POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest tests/test_auth_type_system.py tests/test_project_schema_postgres.py tests/test_initialize_admin.py -q`

预期：全部通过。

- [ ] **步骤 6：提交**

```bash
git add backend/packages/harness/deerflow/persistence backend/app/gateway/auth backend/app/gateway/routers/auth.py backend/app/gateway/app.py backend/tests/test_auth_type_system.py backend/tests/test_project_schema_postgres.py backend/tests/test_initialize_admin.py
git commit -m "feat(projects): add project foundation schema"
```

### 任务 7：能力模型和 `ProjectContext`

**文件：**

- 新建：`backend/app/projects/__init__.py`
- 新建：`backend/app/projects/capabilities.py`
- 新建：`backend/app/projects/context.py`
- 新建：`backend/app/projects/errors.py`
- 新建：`backend/app/projects/models.py`
- 新建：`backend/tests/test_project_capabilities.py`
- 新建：`backend/tests/test_project_context.py`

**接口：**

- `capabilities_for(role: ProjectRole) -> frozenset[Capability]`。
- `resolve_project_context(session, user_id, project_id, request_id) -> ProjectContext`。
- 后续 service 和 router 只接收该不可变上下文。

- [ ] **步骤 1：编写能力矩阵和拒绝测试**

```python
def test_viewer_cannot_execute_or_update():
    capabilities = capabilities_for(ProjectRole.VIEWER)
    assert Capability.PROJECT_READ in capabilities
    assert Capability.PRIVATE_WORK_CREATE not in capabilities
    assert Capability.PROJECT_UPDATE not in capabilities


@pytest.mark.asyncio
async def test_resolve_context_hides_non_member_project(session, users, projects):
    with pytest.raises(ProjectNotFound):
        await resolve_project_context(session, users.outsider.id, projects.alpha.id, "req-1")
```

- [ ] **步骤 2：运行确认失败**

运行：`cd backend && uv run pytest tests/test_project_capabilities.py tests/test_project_context.py -q`

预期：模块不存在。

- [ ] **步骤 3：实现枚举和不可变上下文**

```python
class ProjectRole(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    RUNNER = "runner"
    VIEWER = "viewer"


class Capability(StrEnum):
    PROJECT_READ = "project.read"
    PROJECT_UPDATE = "project.update"
    PROJECT_ENTER = "project.enter"
    PROJECT_PIN = "project.pin"
    PROJECT_MEMBERS_MANAGE = "project.members.manage"
    PRIVATE_WORK_CREATE = "private_work.create"
    PRIVATE_WORK_READ_OWN = "private_work.read_own"
    AUTOMATION_MANAGE_OWN = "automation.manage_own"
    SHARED_ASSETS_READ = "shared_assets.read"
    SHARED_ASSETS_EXECUTE = "shared_assets.execute"
    SHARED_ASSETS_EDIT = "shared_assets.edit"
    MCP_CREDENTIALS_APPROVE = "mcp.credentials.approve"
    PROJECT_AUDIT_READ = "project.audit.read"
    PROJECT_USAGE_READ = "project.usage.read"
    PROJECT_LIFECYCLE_MANAGE = "project.lifecycle.manage"


@dataclass(frozen=True)
class ProjectContext:
    user_id: uuid.UUID
    project_id: uuid.UUID
    membership_id: uuid.UUID
    role: ProjectRole
    capabilities: frozenset[Capability]
    membership_version: int
    request_id: str

    def require(self, capability: Capability) -> None:
        if capability not in self.capabilities:
            raise ProjectForbidden(capability.value)
```

角色映射必须显式列出上述全部能力：Admin 拥有全部能力；Editor 不拥有成员、凭据、审计、用量和生命周期能力；Runner 只拥有读取共享资产、执行共享资产和管理自己私有工作的能力；Viewer 只拥有项目读取、进入、置顶、共享资产读取和读取自己既有私有数据的能力。

- [ ] **步骤 4：实现上下文解析**

查询必须在一个 statement 中同时约束 project ID、user ID、membership `active`、project `active` 和 `is_suspended=false`。非成员、不存在、暂停或非 active 统一抛出 `ProjectNotFound`，避免泄露。

- [ ] **步骤 5：运行测试并提交**

运行：`cd backend && uv run pytest tests/test_project_capabilities.py tests/test_project_context.py -q`

预期：全部通过。

```bash
git add backend/app/projects backend/tests/test_project_capabilities.py backend/tests/test_project_context.py
git commit -m "feat(projects): define capabilities and project context"
```

### 任务 8：项目仓储和业务服务

**文件：**

- 新建：`backend/app/projects/repository.py`
- 新建：`backend/app/projects/service.py`
- 新建：`backend/tests/test_project_repository_postgres.py`
- 新建：`backend/tests/test_project_service.py`

**接口：**

- `ProjectRepository.create_with_admin(user_id, command) -> ProjectContext`。
- `ProjectRepository.list_for_user(user_id, query, pinned, cursor, limit) -> ProjectPage`。
- `ProjectService.update(context, changes) -> ProjectView`。
- 所有修改操作在一个 PostgreSQL transaction 中完成。

- [ ] **步骤 1：编写原子创建和作用域测试**

```python
@pytest.mark.asyncio
async def test_create_project_is_atomic(repository, user):
    context = await repository.create_with_admin(user.id, CreateProject(slug="alpha", display_name="Alpha"))
    assert context.role is ProjectRole.ADMIN
    assert await repository.membership_count(context.project_id) == 1


@pytest.mark.asyncio
async def test_get_project_uses_membership_scope(repository, outsider, project):
    assert await repository.get_for_user(outsider.id, project.id) is None
```

- [ ] **步骤 2：运行确认失败**

运行：`cd backend && POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest tests/test_project_repository_postgres.py tests/test_project_service.py -q`

预期：repository/service 不存在。

- [ ] **步骤 3：实现 slug 规范化和项目创建**

```python
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_slug(value: str) -> str:
    normalized = value.strip().lower()
    if not 3 <= len(normalized) <= 63 or not SLUG_PATTERN.fullmatch(normalized):
        raise ProjectValidationFailed("invalid_slug")
    return normalized
```

`create_with_admin()` 在同一 `session.begin()` 中插入 project 和 Admin membership；捕获 slug unique violation 并转换为 `ProjectSlugConflict`。

- [ ] **步骤 4：实现列表、详情、更新、进入和置顶**

- 列表只从 `project_memberships.user_id=:user_id AND status='active'` 出发 join project。
- 排序为 `is_pinned DESC, last_entered_at DESC NULLS LAST, project.created_at DESC, project.id DESC`。
- 游标编码上述排序字段，不使用 offset。
- update 先调用 `context.require(PROJECT_UPDATE)`，只允许 `display_name`、`description`、`icon`。
- enter 只更新当前 membership 的 `last_entered_at`。
- pin 只更新当前 membership 的 `is_pinned`。

- [ ] **步骤 5：运行仓储与并发测试**

运行：`cd backend && POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest tests/test_project_repository_postgres.py tests/test_project_service.py -q`

预期：全部通过，包括并发 slug 冲突、非成员 404 语义、角色 403 语义和个人置顶隔离。

- [ ] **步骤 6：提交**

```bash
git add backend/app/projects/repository.py backend/app/projects/service.py backend/tests/test_project_repository_postgres.py backend/tests/test_project_service.py
git commit -m "feat(projects): add scoped project service"
```

### 任务 9：项目 API 和默认项目初始化

**文件：**

- 新建：`backend/app/gateway/routers/projects.py`
- 修改：`backend/app/gateway/routers/__init__.py`
- 修改：`backend/app/gateway/app.py`
- 修改：`backend/app/gateway/deps.py`
- 修改：`backend/app/gateway/routers/auth.py`
- 修改：`backend/scripts/setup_postgres.py`
- 新建：`backend/tests/test_projects_router.py`
- 新建：`backend/tests/test_default_project_bootstrap.py`

**接口：**

- API：`POST/GET /api/projects`、`GET/PATCH /api/projects/{project_id}`、`POST /enter`、`PUT /pin`。
- 初始化：`bootstrap_default_project(session) -> BootstrapResult`。

- [ ] **步骤 1：编写 API 契约测试**

```python
@pytest.mark.asyncio
async def test_outsider_gets_project_not_found(client, outsider_cookie, project):
    response = await client.get(f"/api/projects/{project.id}", cookies=outsider_cookie)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


@pytest.mark.asyncio
async def test_editor_cannot_patch_project(client, editor_cookie, project):
    response = await client.patch(
        f"/api/projects/{project.id}",
        cookies=editor_cookie,
        json={"display_name": "Changed"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PROJECT_FORBIDDEN"
```

- [ ] **步骤 2：运行确认路由不存在**

运行：`cd backend && uv run pytest tests/test_projects_router.py tests/test_default_project_bootstrap.py -q`

预期：404 或 import 失败。

- [ ] **步骤 3：实现请求和响应模型**

```python
class ProjectCreateRequest(BaseModel):
    slug: str
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    icon: str = Field(default="folder", max_length=32)


class ProjectResponse(BaseModel):
    id: uuid.UUID
    slug: str
    display_name: str
    description: str
    icon: str
    role: ProjectRole
    capabilities: list[Capability]
    is_pinned: bool
    last_entered_at: datetime | None
    member_count: int
    agent_count: int = 0
    skill_count: int = 0
    mcp_count: int = 0
    status: str
    is_suspended: bool
    membership_version: int
    request_id: str
```

- [ ] **步骤 4：注册路由和依赖**

路由只从认证上下文取 user ID；详情、更新、进入和置顶先解析 `ProjectContext`。领域异常统一映射到稳定公共错误码，不返回 SQL 错误。

- [ ] **步骤 5：实现默认项目初始化**

- 没有用户：只完成 schema。
- 恰好一个 `system_admin`：幂等创建 `default-project` 和 Admin membership。
- 多用户且没有唯一 `system_admin`：停止并返回 `AMBIGUOUS_BOOTSTRAP_ADMIN`。
- `/initialize` 创建首个 `system_admin` 后调用同一 service。

- [ ] **步骤 6：运行 API 和初始化测试**

运行：`cd backend && POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest tests/test_projects_router.py tests/test_default_project_bootstrap.py tests/test_auth.py -q`

预期：全部通过。

- [ ] **步骤 7：提交**

```bash
git add backend/app/gateway/routers/projects.py backend/app/gateway/routers/__init__.py backend/app/gateway/app.py backend/app/gateway/deps.py backend/app/gateway/routers/auth.py backend/scripts/setup_postgres.py backend/tests/test_projects_router.py backend/tests/test_default_project_bootstrap.py
git commit -m "feat(projects): expose project APIs and bootstrap"
```

---

## 第三交付段：前端项目体验与发布门禁

### 任务 10：前端项目契约、查询键和账户隔离

**文件：**

- 新建：`frontend/src/core/projects/types.ts`
- 新建：`frontend/src/core/projects/api.ts`
- 新建：`frontend/src/core/projects/query-keys.ts`
- 新建：`frontend/src/core/projects/hooks.ts`
- 新建：`frontend/src/core/projects/index.ts`
- 修改：`frontend/src/core/auth/types.ts`
- 修改：`frontend/src/core/auth/static-user.ts`
- 修改：`frontend/src/core/auth/auth-disabled-user.ts`
- 修改：`frontend/src/core/auth/AuthProvider.tsx`
- 修改：`frontend/src/components/query-client-provider.tsx`
- 新建：`frontend/tests/unit/core/projects/types.test.ts`
- 新建：`frontend/tests/unit/core/projects/query-keys.test.ts`
- 新建：`frontend/tests/unit/core/projects/api.test.ts`

**接口：**

- `accountProjectsKey(userId, filters)`。
- `projectDetailKey(userId, projectId)`。
- `useProjects(userId, filters)`、`useCreateProject(userId)`、`useProject(userId, projectId)`。

- [ ] **步骤 1：编写 schema 和查询键测试**

```typescript
it("scopes project detail by account and project", () => {
  expect(projectDetailKey("u1", "p1")).toEqual([
    "account",
    "u1",
    "project",
    "p1",
    "detail",
  ]);
});

it("accepts system_admin and rejects legacy admin", () => {
  expect(userSchema.safeParse({ id: "u1", email: "a@example.com", system_role: "system_admin" }).success).toBe(true);
  expect(userSchema.safeParse({ id: "u1", email: "a@example.com", system_role: "admin" }).success).toBe(false);
});
```

- [ ] **步骤 2：运行确认失败**

运行：`cd frontend && pnpm test -- projects types.test.ts`

预期：project 模块不存在，legacy role 测试失败。

- [ ] **步骤 3：实现 Zod 契约和 API**

```typescript
export const projectSchema = z.object({
  id: z.string().uuid(),
  slug: z.string(),
  display_name: z.string(),
  description: z.string(),
  icon: z.string(),
  role: z.enum(["admin", "editor", "runner", "viewer"]),
  capabilities: z.array(z.string()),
  is_pinned: z.boolean(),
  last_entered_at: z.string().datetime().nullable(),
  member_count: z.number().int().nonnegative(),
  agent_count: z.number().int().nonnegative(),
  skill_count: z.number().int().nonnegative(),
  mcp_count: z.number().int().nonnegative(),
  status: z.literal("active"),
  is_suspended: z.boolean(),
  membership_version: z.number().int().positive(),
  request_id: z.string(),
});
```

所有请求使用现有带 CSRF 的 `fetch` wrapper，并在解析失败时抛出领域化 `ProjectApiError`。

- [ ] **步骤 4：让 QueryClient 按账户清理**

将模块级 singleton 改为 provider 实例拥有的 client。`AuthProvider.logout()` 和 `applyUser()` 账户变化时调用 `queryClient.clear()`；同时取消进行中的项目请求。测试账户从 `u1` 切换 `u2` 后无法读取旧缓存。

- [ ] **步骤 5：运行前端领域测试**

运行：`cd frontend && pnpm test -- projects auth query-client`

预期：全部通过。

- [ ] **步骤 6：提交**

```bash
git add frontend/src/core/projects frontend/src/core/auth frontend/src/components/query-client-provider.tsx frontend/tests/unit/core/projects
git commit -m "feat(frontend): add account-scoped project data"
```

### 任务 11：项目工作台和项目主页

**文件：**

- 新建：`frontend/src/app/workspace/projects/page.tsx`
- 新建：`frontend/src/app/projects/[project_slug]/page.tsx`
- 新建：`frontend/src/components/projects/project-workbench.tsx`
- 新建：`frontend/src/components/projects/project-card.tsx`
- 新建：`frontend/src/components/projects/create-project-dialog.tsx`
- 新建：`frontend/src/components/projects/project-empty-state.tsx`
- 新建：`frontend/src/components/projects/project-home.tsx`
- 新建：`frontend/src/components/projects/project-header.tsx`
- 新建：`frontend/src/components/projects/project-private-work-cta.tsx`
- 修改：`frontend/src/app/workspace/page.tsx`
- 修改：`frontend/src/components/workspace/workspace-sidebar.tsx`
- 修改：`frontend/src/components/workspace/workspace-header.tsx`
- 修改：`frontend/src/components/workspace/command-palette.tsx`
- 新建：`frontend/tests/unit/components/projects/project-card.test.tsx`
- 新建：`frontend/tests/unit/components/projects/project-workbench.test.tsx`
- 新建：`frontend/tests/unit/components/projects/project-home.test.tsx`
- 新建：`frontend/tests/e2e/projects.spec.ts`

**接口：**

- 工作台使用任务 10 hooks。
- 项目主页通过 slug 获取项目后调用 `/enter`。
- `project_private_workspace=false` 时 CTA 不导航、不创建 Thread。

- [ ] **步骤 1：编写工作台交互和功能门禁测试**

```typescript
it("orders pinned projects first and exposes create action", async () => {
  render(<ProjectWorkbench userId="u1" />);
  expect(await screen.findAllByTestId("project-card")).toHaveLength(2);
  expect(screen.getAllByTestId("project-card")[0]).toHaveTextContent("Pinned");
  expect(screen.getByRole("button", { name: "创建项目" })).toBeVisible();
});

it("does not create a thread while private workspace is disabled", async () => {
  render(<ProjectPrivateWorkCta enabled={false} />);
  await userEvent.click(screen.getByRole("button", { name: "开始私有对话" }));
  expect(screen.getByText("私有工作区将在后续里程碑开放")).toBeVisible();
  expect(mockRouterPush).not.toHaveBeenCalled();
});
```

- [ ] **步骤 2：运行确认页面和组件不存在**

运行：`cd frontend && pnpm test -- project-card project-workbench project-home`

预期：import 失败。

- [ ] **步骤 3：实现工作台状态**

工作台必须实现：加载骨架屏、无项目、搜索无结果、API 错误重试、卡片网格、创建、置顶、进入、深色模式、移动布局、键盘焦点。项目卡片不显示成员私有活动。

- [ ] **步骤 4：实现项目主页和 shell**

项目主页显示项目标识、角色、隐私边界提示、三个共享资产计数入口和禁用的私有对话 CTA。项目内导航只提供返回工作台，不提供项目下拉切换器。

- [ ] **步骤 5：修改默认路由和导航**

- `/workspace` 重定向 `/workspace/projects`。
- sidebar 和 command palette 的主要入口指向项目工作台。
- legacy 对话入口只在非项目优先功能配置下保留。
- static demo 模式继续进入 demo Thread，不调用项目 API。

- [ ] **步骤 6：运行单元和 E2E**

运行：

```bash
cd frontend
pnpm test -- project-card project-workbench project-home
pnpm test:e2e -- projects.spec.ts
```

预期：单元和 E2E 全部通过，E2E 覆盖创建、搜索、置顶、进入、返回、错误、空状态、移动和深色模式。

- [ ] **步骤 7：提交**

```bash
git add frontend/src/app/workspace frontend/src/app/projects frontend/src/components/projects frontend/src/components/workspace frontend/tests/unit/components/projects frontend/tests/e2e/projects.spec.ts
git commit -m "feat(frontend): add project workbench and home"
```

### 任务 12：端到端迁移门禁、文档和 M1 验收

**文件：**

- 新建：`backend/tests/integration/test_m1_postgres_cutover.py`
- 新建：`backend/tests/integration/test_project_isolation_postgres.py`
- 新建：`.github/workflows/project-foundation-postgres-tests.yml`
- 修改：`README.md`
- 修改：`AGENTS.md`
- 修改：`backend/AGENTS.md`
- 修改：`frontend/AGENTS.md`
- 修改：`Install.md`
- 修改：`scripts/doctor.py`
- 修改：`scripts/check.py`

**接口：**

- CI 使用 PostgreSQL service container，设置 `POSTGRES_TEST_URL`。
- 验收必须证明 SQLite 不再是运行依赖、迁移数据一致、项目 API 隔离成立。

- [ ] **步骤 1：编写 M1 完整迁移测试**

```python
@pytest.mark.asyncio
async def test_m1_cutover_preserves_legacy_data_and_bootstraps_project(legacy_snapshot, postgres_admin_url):
    report = await run_full_cutover(legacy_snapshot, postgres_admin_url)
    assert report.migration.verified is True
    assert report.runtime_backend == "postgresql"
    assert report.default_project.slug == "default-project"
    assert report.default_membership.role == "admin"
    assert await legacy_source_is_unchanged(legacy_snapshot)
```

- [ ] **步骤 2：编写隔离矩阵测试**

```python
@pytest.mark.asyncio
async def test_project_isolation_matrix(project_client, matrix):
    assert (await project_client(matrix.alpha_admin).get(matrix.alpha.id)).status_code == 200
    assert (await project_client(matrix.alpha_viewer).patch(matrix.alpha.id, name="x")).status_code == 403
    assert (await project_client(matrix.beta_admin).get(matrix.alpha.id)).status_code == 404
    assert (await project_client(matrix.outsider).get(matrix.alpha.id)).status_code == 404
```

- [ ] **步骤 3：运行确认门禁失败**

运行：`cd backend && POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest tests/integration/test_m1_postgres_cutover.py tests/integration/test_project_isolation_postgres.py -q`

预期：测试失败，提示 `run_full_cutover`、项目矩阵 fixture 或相应最终门禁尚未实现。

- [ ] **步骤 4：增加 CI PostgreSQL service**

workflow 启动 PostgreSQL，创建维护账号，安装默认 backend 依赖，运行 migration、迁移器测试、项目后端测试和完整隔离矩阵。不得将真实本地密码写入 workflow。

- [ ] **步骤 5：更新 doctor、安装和架构文档**

- README 和 Install：说明本地 Docker PostgreSQL、`DATABASE_URL`、`make setup-db`、`make migrate-sqlite` 和备份流程。
- 根/后端 AGENTS：记录 PostgreSQL-only、不使用 RLS、作用域仓储、测试命令和项目模块边界。
- 前端 AGENTS：记录项目路由、查询键和功能门禁。
- doctor/check：验证 Docker 暴露的 PostgreSQL、数据库连接、migration head 和密码脱敏。

- [ ] **步骤 6：运行完整验证**

运行：

```bash
make check-db
cd backend && POSTGRES_TEST_URL="$DATABASE_URL" make test
cd backend && make lint
cd frontend && pnpm test
cd frontend && pnpm check
cd frontend && pnpm test:e2e -- projects.spec.ts
git diff --check
rg -n "sqlite\+aiosqlite|langgraph-checkpoint-sqlite|database\.backend.*sqlite|SQLiteUserRepository" backend config.example.yaml scripts
```

预期：

- 所有测试和检查退出码为 0；
- 最后一条 `rg` 无匹配，唯一允许的 SQLite 运行库引用是 `backend/scripts/sqlite_inventory.py`、`backend/scripts/migrate_sqlite_to_postgres.py` 及其测试中的标准库 `sqlite3`；
- `make check-db` 不输出密码；
- 项目 E2E 全部通过。

- [ ] **步骤 7：提交**

```bash
git add .github/workflows/project-foundation-postgres-tests.yml README.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md Install.md scripts/doctor.py scripts/check.py backend/tests/integration
git commit -m "test(projects): enforce M1 PostgreSQL release gates"
```

---

## 实施结束条件

只有任务 1 至 12 全部完成，并且任务 12 的完整验证命令全部通过，才能把 M1 状态从“未开始”改为“已完成”。如果受外部环境影响无法运行真实 PostgreSQL、完整后端测试或 Playwright，M1 必须保持未完成，并在交付记录中列出具体未执行门禁。

## 规格覆盖检查

| M1 规格要求 | 实施任务 |
| --- | --- |
| 决策冻结、威胁模型和数据清单 | 任务 1、12 |
| SQLite 只读备份、清点、原样迁移和校验 | 任务 1、5、12 |
| PostgreSQL-only 配置、engine、checkpointer 和测试 | 任务 2、3、4 |
| 数据库创建、检查、migration 和 Makefile 入口 | 任务 4 |
| 平台 `system_admin` 和项目 schema | 任务 6 |
| 四种角色、能力模型和 `ProjectContext` | 任务 7 |
| 作用域仓储、事务和项目不变量 | 任务 8 |
| 项目创建、列表、详情、更新、进入、置顶和默认项目 | 任务 9 |
| 前端契约、账户/项目查询键和缓存清理 | 任务 10 |
| 项目工作台、项目主页、响应式、深色模式和错误状态 | 任务 11 |
| `project_private_workspace` 默认关闭 | 任务 10、11、12 |
| PostgreSQL 集成测试、E2E、CI、文档和发布门禁 | 任务 12 |

自检未发现未覆盖的 M1 规格项。M2 之后的邀请、成员变更、共享资产版本、私有数据项目化、任务租约、配额和审计没有被提前加入本计划。

执行完成后使用 `superpowers:requesting-code-review` 进行独立审查，再使用 `superpowers:finishing-a-development-branch` 决定合并、PR 或保留分支。
