"""显式创建并初始化 ActWeave PostgreSQL 数据库。"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg import AsyncConnection, sql
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.projects.errors import ProjectBootstrapFailed
from app.system_runtime_settings.bootstrap import (
    SystemRuntimePolicyBootstrapConflict,
    SystemRuntimePolicyBootstrapStorageUnavailable,
    bootstrap_system_runtime_policies,
)
from app.system_settings.bootstrap import (
    DefaultSystemModelBootstrapConfigurationInvalid,
    DefaultSystemModelBootstrapConflict,
    DefaultSystemModelBootstrapMaterial,
    DefaultSystemModelBootstrapStorageUnavailable,
    bootstrap_default_system_model,
    prepare_default_system_model_bootstrap,
)
from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import (
    CURRENT_SCHEMA_REVISION,
    SchemaRecreateRequired,
    SchemaSetupRequired,
    bootstrap_schema,
)
from deerflow.persistence.final_schema_contract import FINAL_APP_TABLES

BACKEND_ROOT = Path(__file__).resolve().parents[1]

_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_POSTGRESQL_DRIVERS = frozenset({"postgresql", "postgresql+asyncpg"})
_DUPLICATE_DATABASE_SQLSTATE = "42P04"
_SETUP_LOCK_KEY = 0x0DEE_12F1_5E7D_0004
_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_5E7D_0005
_BOOTSTRAP_LOCK_POLL_SECONDS = 0.1
_CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u9fff]")
_ALLOWED_LANGGRAPH_TABLES = frozenset(
    {
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_writes",
        "checkpoints",
        "store",
        "store_migrations",
    }
)
_EXPECTED_ROOT_TABLES = FINAL_APP_TABLES | _ALLOWED_LANGGRAPH_TABLES | {"alembic_version"}
_ROOT_TABLE_CATALOG_SQL = """
SELECT namespace.nspname AS schema_name, relation.relname AS table_name
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace
  ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = current_schema()
  AND relation.relkind IN ('r', 'p')
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_depend AS dependency
      WHERE dependency.classid = 'pg_class'::regclass
        AND dependency.objid = relation.oid
        AND dependency.deptype = 'e'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_inherits AS inheritance
      WHERE inheritance.inhrelid = relation.oid
  )
ORDER BY relation.relname
"""
_LANGGRAPH_CATALOG_SQL = """
SELECT namespace.nspname AS schema_name,
       relation.relname AS table_name,
       attribute.attname AS column_name
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace
  ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_attribute AS attribute
  ON attribute.attrelid = relation.oid
WHERE namespace.nspname = current_schema()
  AND relation.relkind IN ('r', 'p')
  AND relation.relname = ANY(%s)
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
ORDER BY relation.relname, attribute.attnum
"""


@dataclass(frozen=True)
class _PostgresTableComments:
    table_name: str
    table_comment: str
    column_comments: tuple[tuple[str, str], ...]


_LANGGRAPH_COMMENT_INVENTORY = (
    _PostgresTableComments(
        table_name="checkpoint_blobs",
        table_comment="LangGraph 检查点各通道版本对应的序列化数据。",
        column_comments=(
            ("thread_id", "所属 LangGraph 线程标识。"),
            ("checkpoint_ns", "检查点命名空间。"),
            ("channel", "状态通道名称。"),
            ("version", "通道数据版本标识。"),
            ("type", "通道数据的序列化类型。"),
            ("blob", "通道数据的序列化二进制内容。"),
        ),
    ),
    _PostgresTableComments(
        table_name="checkpoint_migrations",
        table_comment="LangGraph 检查点表结构的迁移版本记录。",
        column_comments=(("v", "已应用的检查点迁移版本号。"),),
    ),
    _PostgresTableComments(
        table_name="checkpoint_writes",
        table_comment="LangGraph 检查点任务产生的待处理通道写入。",
        column_comments=(
            ("thread_id", "所属 LangGraph 线程标识。"),
            ("checkpoint_ns", "检查点命名空间。"),
            ("checkpoint_id", "写入所属的检查点标识。"),
            ("task_id", "产生写入的任务标识。"),
            ("idx", "同一任务内的写入顺序编号。"),
            ("channel", "写入目标的状态通道名称。"),
            ("type", "写入数据的序列化类型。"),
            ("blob", "写入数据的序列化二进制内容。"),
            ("task_path", "任务在图执行过程中的路径，用于稳定排序。"),
        ),
    ),
    _PostgresTableComments(
        table_name="checkpoints",
        table_comment="LangGraph 线程检查点的序列化状态和元数据。",
        column_comments=(
            ("thread_id", "所属 LangGraph 线程标识。"),
            ("checkpoint_ns", "检查点命名空间。"),
            ("checkpoint_id", "检查点标识。"),
            ("parent_checkpoint_id", "父检查点标识；根检查点为空。"),
            ("type", "检查点的序列化类型兼容字段。"),
            ("checkpoint", "检查点状态的 JSON 数据。"),
            ("metadata", "检查点附加元数据的 JSON 数据。"),
        ),
    ),
    _PostgresTableComments(
        table_name="store",
        table_comment="LangGraph 跨线程存储的命名空间键值数据。",
        column_comments=(
            ("prefix", "存储项命名空间的编码前缀。"),
            ("key", "命名空间内的存储项键。"),
            ("value", "存储项内容的 JSON 数据。"),
            ("created_at", "存储项创建时间。"),
            ("updated_at", "存储项最后更新时间。"),
            ("expires_at", "存储项到期时间；永久有效时为空。"),
            ("ttl_minutes", "存储项的生存时长分钟数；未设置时为空。"),
        ),
    ),
    _PostgresTableComments(
        table_name="store_migrations",
        table_comment="LangGraph 跨线程存储表结构的迁移版本记录。",
        column_comments=(("v", "已应用的跨线程存储迁移版本号。"),),
    ),
)


class PostgresSetupError(RuntimeError):
    """A credential-safe PostgreSQL setup failure."""


@dataclass(frozen=True)
class PostgresTarget:
    host: str
    port: int
    database: str
    username: str


@dataclass(frozen=True)
class SetupResult:
    host: str
    port: int
    database: str
    owner: str
    created: bool
    revision: str


def validate_identifier(name: str, *, kind: str) -> str:
    """Accept a deliberately small, safely quotable PostgreSQL identifier set."""
    if _IDENTIFIER_PATTERN.fullmatch(name) is None:
        raise ValueError(f"invalid PostgreSQL {kind} identifier")
    return name


def parse_target(url: str, *, maintenance: bool = False) -> PostgresTarget:
    """Parse safe connection metadata without retaining credentials."""
    try:
        parsed = make_url(url)
    except Exception:
        raise ValueError("invalid PostgreSQL URL") from None
    if parsed.drivername not in _POSTGRESQL_DRIVERS:
        raise ValueError("URL must use PostgreSQL")
    if not parsed.host or not parsed.database or not parsed.username:
        raise ValueError("PostgreSQL URL must include host, username, and database")
    database = validate_identifier(parsed.database, kind="database")
    username = validate_identifier(parsed.username, kind="role")
    if maintenance and database != "postgres":
        raise ValueError("POSTGRES_ADMIN_URL must connect to the postgres maintenance database")
    return PostgresTarget(
        host=parsed.host,
        port=parsed.port or 5432,
        database=database,
        username=username,
    )


def _asyncpg_url(url: str) -> str:
    """Return a driver-neutral DSN while preserving percent-encoded credentials."""
    try:
        parsed = make_url(url)
    except Exception:
        raise ValueError("invalid PostgreSQL URL") from None
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


async def ensure_database(
    admin_url: str,
    database_name: str = "deerflow",
    *,
    owner_name: str | None = None,
) -> bool:
    """确保数据库存在；新建返回 True，已存在返回 False。"""
    parse_target(admin_url, maintenance=True)
    database_name = validate_identifier(database_name, kind="database")
    if owner_name is not None:
        owner_name = validate_identifier(owner_name, kind="role")

    connection = None
    lock_acquired = False
    try:
        connection = await asyncpg.connect(_asyncpg_url(admin_url))
        await connection.execute("SELECT pg_advisory_lock($1)", _SETUP_LOCK_KEY)
        lock_acquired = True
        if owner_name is not None and not await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", owner_name):
            raise PostgresSetupError("目标 PostgreSQL role 不存在；请先由管理员创建该 role，再重试 setup-db")
        if await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database_name):
            return False

        owner_clause = f' OWNER "{owner_name}"' if owner_name is not None else ""
        try:
            # A setup target must be genuinely empty.  ``template1`` may carry
            # deployment-specific extensions (for example TimescaleDB background
            # workers), so inherit only PostgreSQL's pristine template0.
            await connection.execute(f'CREATE DATABASE "{database_name}"{owner_clause} TEMPLATE template0')
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != _DUPLICATE_DATABASE_SQLSTATE:
                raise
            if await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database_name):
                return False
            raise
        return True
    except PostgresSetupError:
        raise
    except Exception:
        raise PostgresSetupError("无法确保 PostgreSQL 数据库存在；请检查 POSTGRES_ADMIN_URL、管理员权限和目标 role") from None
    finally:
        if connection is not None:
            if lock_acquired:
                try:
                    await connection.execute("SELECT pg_advisory_unlock($1)", _SETUP_LOCK_KEY)
                except Exception:
                    pass
            try:
                await connection.close()
            except Exception:
                pass


async def _database_exists(admin_url: str, database_name: str) -> bool:
    connection = None
    try:
        connection = await asyncpg.connect(_asyncpg_url(admin_url))
        return bool(
            await connection.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1",
                database_name,
            )
        )
    except Exception:
        raise PostgresSetupError(
            "无法检查 PostgreSQL 数据库状态；请检查 POSTGRES_ADMIN_URL",
        ) from None
    finally:
        if connection is not None:
            await connection.close()


def _create_setup_engine(config: DatabaseConfig) -> AsyncEngine:
    """Create an engine owned only by one setup invocation."""
    return create_async_engine(
        config.sqlalchemy_url,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_timeout=config.pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={"server_settings": {"statement_timeout": str(config.statement_timeout_seconds * 1000)}},
    )


async def _bootstrap_langgraph_schemas(database_url: str) -> None:
    """Idempotently initialize and document the exact LangGraph table set."""
    connection_url = _asyncpg_url(database_url)
    try:
        async with AsyncPostgresSaver.from_conn_string(connection_url) as saver:
            await saver.setup()
        async with AsyncPostgresStore.from_conn_string(connection_url) as store:
            await store.setup()
        await _comment_langgraph_schemas(connection_url)
    except PostgresSetupError:
        raise
    except Exception:
        raise PostgresSetupError("LangGraph PostgreSQL schema 初始化失败；请检查 DATABASE_URL、目标 role 权限和数据库状态") from None


def _validated_langgraph_comment_columns() -> dict[str, tuple[str, ...]]:
    """Validate the closed comment inventory before constructing any DDL."""
    table_names = tuple(item.table_name for item in _LANGGRAPH_COMMENT_INVENTORY)
    if len(table_names) != len(set(table_names)) or set(table_names) != _ALLOWED_LANGGRAPH_TABLES:
        raise PostgresSetupError("LangGraph PostgreSQL 注释清单无效；请检查允许的表和字段清单")

    expected_columns: dict[str, tuple[str, ...]] = {}
    for item in _LANGGRAPH_COMMENT_INVENTORY:
        validate_identifier(item.table_name, kind="table")
        if not item.table_comment.strip() or _CHINESE_TEXT_PATTERN.search(item.table_comment) is None:
            raise PostgresSetupError("LangGraph PostgreSQL 注释清单无效；请检查允许的表和字段清单")

        column_names = tuple(column_name for column_name, _comment in item.column_comments)
        if not column_names or len(column_names) != len(set(column_names)):
            raise PostgresSetupError("LangGraph PostgreSQL 注释清单无效；请检查允许的表和字段清单")
        for column_name, comment in item.column_comments:
            validate_identifier(column_name, kind="column")
            if not comment.strip() or _CHINESE_TEXT_PATTERN.search(comment) is None:
                raise PostgresSetupError("LangGraph PostgreSQL 注释清单无效；请检查允许的表和字段清单")
        expected_columns[item.table_name] = column_names
    return expected_columns


async def _comment_langgraph_schemas(connection_url: str) -> None:
    """Apply table and column comments atomically after LangGraph setup."""
    try:
        expected_columns = _validated_langgraph_comment_columns()
        async with await AsyncConnection.connect(connection_url) as connection:
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(_ROOT_TABLE_CATALOG_SQL)
                    root_rows = await cursor.fetchall()
                    root_schemas = {schema_name for schema_name, _table_name in root_rows}
                    root_table_names = [table_name for _schema_name, table_name in root_rows]
                    if len(root_schemas) != 1 or len(root_table_names) != len(set(root_table_names)) or set(root_table_names) != _EXPECTED_ROOT_TABLES:
                        raise PostgresSetupError("LangGraph PostgreSQL schema 与允许的注释清单不一致；请检查依赖版本和数据库状态")

                    schema_name = root_schemas.pop()
                    if not isinstance(schema_name, str) or not schema_name:
                        raise PostgresSetupError("LangGraph PostgreSQL schema 与允许的注释清单不一致；请检查依赖版本和数据库状态")
                    await cursor.execute(
                        _LANGGRAPH_CATALOG_SQL,
                        (sorted(_ALLOWED_LANGGRAPH_TABLES),),
                    )
                    rows = await cursor.fetchall()
                    schemas = {schema_name for schema_name, _table_name, _column_name in rows}
                    actual_columns: dict[str, list[str]] = {}
                    for _schema_name, table_name, column_name in rows:
                        actual_columns.setdefault(table_name, []).append(column_name)

                    if schemas != {schema_name} or {table_name: tuple(column_names) for table_name, column_names in actual_columns.items()} != expected_columns:
                        raise PostgresSetupError("LangGraph PostgreSQL schema 与允许的注释清单不一致；请检查依赖版本和数据库状态")

                    for item in _LANGGRAPH_COMMENT_INVENTORY:
                        await cursor.execute(
                            sql.SQL("COMMENT ON TABLE {} IS {}").format(
                                sql.Identifier(schema_name, item.table_name),
                                sql.Literal(item.table_comment),
                            )
                        )
                        for column_name, comment in item.column_comments:
                            await cursor.execute(
                                sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
                                    sql.Identifier(schema_name, item.table_name),
                                    sql.Identifier(column_name),
                                    sql.Literal(comment),
                                )
                            )
    except PostgresSetupError:
        raise
    except Exception:
        raise PostgresSetupError("LangGraph PostgreSQL 表和字段注释写入失败；请检查 DATABASE_URL、目标 role 权限和数据库状态") from None


def _create_bootstrap_lock_engine(database_url: str) -> AsyncEngine:
    """Create a dedicated non-pooled engine for setup coordination only."""
    config = DatabaseConfig(url=database_url)
    return create_async_engine(
        config.sqlalchemy_url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )


@asynccontextmanager
async def _complete_bootstrap_lock(database_url: str):
    """Serialize ORM and LangGraph setup without application query timeouts."""
    lock_engine = _create_bootstrap_lock_engine(database_url)
    try:
        async with lock_engine.connect() as connection:
            await connection.execute(text("SET statement_timeout = 0"))
            await connection.execute(text("SET idle_in_transaction_session_timeout = 0"))
            idle_session_timeout = await connection.scalar(text("SELECT current_setting('idle_session_timeout', true)"))
            if idle_session_timeout is not None:
                await connection.execute(text("SET idle_session_timeout = 0"))
            while not await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": _BOOTSTRAP_LOCK_KEY},
            ):
                await asyncio.sleep(_BOOTSTRAP_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                try:
                    await connection.scalar(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": _BOOTSTRAP_LOCK_KEY},
                    )
                except Exception:
                    # Closing the dedicated session releases a session lock.
                    pass
    finally:
        await lock_engine.dispose()


async def _bootstrap_existing(
    database_url: str,
    *,
    default_model_bootstrap: (DefaultSystemModelBootstrapMaterial | None) = None,
) -> str:
    engine = _create_setup_engine(DatabaseConfig(url=database_url))
    primary_error: BaseException | None = None
    try:
        async with _complete_bootstrap_lock(database_url):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            await bootstrap_schema(engine)
            await _bootstrap_builtin_catalog(engine)
            await _bootstrap_default_model_schema(
                engine,
                default_model_bootstrap,
            )
            await _bootstrap_runtime_policy_schema(engine)
            await _bootstrap_langgraph_schemas(database_url)
            await _bootstrap_default_project_schema(engine)
        return CURRENT_SCHEMA_REVISION
    except ProjectBootstrapFailed as exc:
        primary_error = exc
        raise PostgresSetupError(exc.code) from None
    except (
        DefaultSystemModelBootstrapConflict,
        DefaultSystemModelBootstrapStorageUnavailable,
        SystemRuntimePolicyBootstrapConflict,
        SystemRuntimePolicyBootstrapStorageUnavailable,
    ) as exc:
        primary_error = exc
        raise PostgresSetupError(str(exc)) from None
    except SchemaRecreateRequired as exc:
        primary_error = exc
        raise PostgresSetupError(f"SCHEMA_RECREATE_REQUIRED: 非空目标库不是完整的 {CURRENT_SCHEMA_REVISION}；请显式重建目标数据库") from None
    except SchemaSetupRequired as exc:
        primary_error = exc
        raise PostgresSetupError("DATABASE_SETUP_REQUIRED: 目标库尚未初始化；请运行 `make setup-db`") from None
    except PostgresSetupError as exc:
        # Nested bootstrap stages already expose credential-safe operator
        # guidance; retain it instead of collapsing it into an unrelated
        # generic schema error.
        primary_error = exc
        raise
    except RuntimeError as exc:
        primary_error = exc
        raise PostgresSetupError("PostgreSQL schema 初始化失败；请检查 DATABASE_URL、目标 role 权限和完整 schema 快照") from None
    except Exception as exc:
        primary_error = exc
        raise PostgresSetupError("PostgreSQL schema 初始化失败；请检查 DATABASE_URL、目标 role 权限和完整 schema 快照") from None
    finally:
        try:
            await engine.dispose()
        except Exception:
            if primary_error is None:
                raise PostgresSetupError("PostgreSQL engine 清理失败；请确认没有其他初始化任务仍在运行") from None


async def _bootstrap_builtin_catalog(engine: AsyncEngine) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.shared_assets.bootstrap import bootstrap_system_assets

    await bootstrap_system_assets(async_sessionmaker(engine, expire_on_commit=False))


async def _bootstrap_default_project_schema(engine: AsyncEngine) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.projects.bootstrap import bootstrap_default_project

    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await bootstrap_default_project(session)


async def _bootstrap_default_model_schema(
    engine: AsyncEngine,
    material: DefaultSystemModelBootstrapMaterial | None,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await bootstrap_default_system_model(
        async_sessionmaker(engine, expire_on_commit=False),
        material,
    )


async def _bootstrap_runtime_policy_schema(engine: AsyncEngine) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await bootstrap_system_runtime_policies(
        async_sessionmaker(engine, expire_on_commit=False),
    )


async def setup_postgres(
    admin_url: str,
    database_url: str,
    *,
    expected_database: str | None = None,
) -> SetupResult:
    """幂等创建目标数据库，并安装或验证完整 schema 快照。"""
    parse_target(admin_url, maintenance=True)
    target = parse_target(database_url)
    if expected_database is not None:
        expected_database = validate_identifier(expected_database, kind="database")
        if target.database != expected_database:
            raise ValueError("DATABASE_URL database does not match --database")
    default_model_bootstrap = None
    existed = await _database_exists(admin_url, target.database)
    if not existed:
        try:
            default_model_bootstrap = prepare_default_system_model_bootstrap()
        except DefaultSystemModelBootstrapConfigurationInvalid as exc:
            raise PostgresSetupError(str(exc)) from None
    created = await ensure_database(
        admin_url,
        target.database,
        owner_name=target.username,
    )
    if default_model_bootstrap is None and (created or await _requires_initial_model_bootstrap(database_url)):
        try:
            default_model_bootstrap = prepare_default_system_model_bootstrap()
        except DefaultSystemModelBootstrapConfigurationInvalid as exc:
            raise PostgresSetupError(str(exc)) from None
    revision = await _bootstrap_existing(
        database_url,
        default_model_bootstrap=default_model_bootstrap,
    )
    return SetupResult(
        host=target.host,
        port=target.port,
        database=target.database,
        owner=target.username,
        created=created,
        revision=revision,
    )


async def _requires_initial_model_bootstrap(database_url: str) -> bool:
    """Inspect only catalog presence before deciding whether Keys are required."""

    engine = _create_setup_engine(DatabaseConfig(url=database_url))
    try:
        async with engine.connect() as connection:
            table = await connection.scalar(text("SELECT to_regclass('system_model_configs')"))
            if table is None:
                return True
            count = await connection.scalar(text("SELECT count(*) FROM system_model_configs"))
            return not isinstance(count, int) or count == 0
    finally:
        await engine.dispose()


def print_result(result: SetupResult) -> None:
    action = "已创建并初始化" if result.created else "已存在并完成初始化"
    print(f"PostgreSQL 数据库{action}")
    print(f"主机: {result.host}:{result.port}")
    print(f"数据库: {result.database}")
    print(f"Schema marker: {result.revision}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="初始化 ActWeave PostgreSQL 数据库")
    parser.add_argument("--database", help="必须与 DATABASE_URL 中的数据库名一致")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须显式设置 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        admin_url = os.getenv("POSTGRES_ADMIN_URL")
        if not admin_url:
            print("错误: setup-db 必须显式设置 POSTGRES_ADMIN_URL", file=sys.stderr)
            return 2
        result = asyncio.run(
            setup_postgres(
                admin_url,
                database_url,
                expected_database=args.database,
            )
        )
        print_result(result)
        return 0
    except (PostgresSetupError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
