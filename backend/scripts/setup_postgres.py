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
    M7RecreateRequired,
    SchemaSetupRequired,
    bootstrap_schema,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_POSTGRESQL_DRIVERS = frozenset({"postgresql", "postgresql+asyncpg"})
_DUPLICATE_DATABASE_SQLSTATE = "42P04"
_SETUP_LOCK_KEY = 0x0DEE_12F1_5E7D_0004
_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_5E7D_0005
_BOOTSTRAP_LOCK_POLL_SECONDS = 0.1


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
            await connection.execute(f'CREATE DATABASE "{database_name}"{owner_clause}')
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
    """Idempotently initialize LangGraph checkpointer and store tables."""
    connection_url = _asyncpg_url(database_url)
    try:
        async with AsyncPostgresSaver.from_conn_string(connection_url) as saver:
            await saver.setup()
        async with AsyncPostgresStore.from_conn_string(connection_url) as store:
            await store.setup()
    except Exception:
        raise PostgresSetupError("LangGraph PostgreSQL schema 初始化失败；请检查 DATABASE_URL、目标 role 权限和数据库状态") from None


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
            if default_model_bootstrap is not None:
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
    except M7RecreateRequired as exc:
        primary_error = exc
        raise PostgresSetupError(f"M7_RECREATE_REQUIRED: 非空目标库不是完整的 {CURRENT_SCHEMA_REVISION}；请显式重建目标数据库") from None
    except SchemaSetupRequired as exc:
        primary_error = exc
        raise PostgresSetupError("DATABASE_SETUP_REQUIRED: 目标库尚未初始化；请运行 `make setup-db`") from None
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
    material: DefaultSystemModelBootstrapMaterial,
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
    try:
        default_model_bootstrap = prepare_default_system_model_bootstrap()
    except DefaultSystemModelBootstrapConfigurationInvalid as exc:
        raise PostgresSetupError(str(exc)) from None
    parse_target(admin_url, maintenance=True)
    target = parse_target(database_url)
    if expected_database is not None:
        expected_database = validate_identifier(expected_database, kind="database")
        if target.database != expected_database:
            raise ValueError("DATABASE_URL database does not match --database")
    created = await ensure_database(
        admin_url,
        target.database,
        owner_name=target.username,
    )
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
