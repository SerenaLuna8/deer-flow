"""显式创建并初始化 DeerFlow PostgreSQL 数据库。"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import _get_head_revision, bootstrap_schema

BACKEND_ROOT = Path(__file__).resolve().parents[1]

_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_POSTGRESQL_DRIVERS = frozenset({"postgresql", "postgresql+asyncpg"})
_DUPLICATE_DATABASE_SQLSTATE = "42P04"
_SETUP_LOCK_KEY = 0x0DEE_12F1_5E7D_0004


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


async def _bootstrap_existing(database_url: str) -> str:
    engine = _create_setup_engine(DatabaseConfig(url=database_url))
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await bootstrap_schema(engine)
        return _get_head_revision()
    except Exception:
        raise PostgresSetupError("PostgreSQL schema 初始化失败；请检查 DATABASE_URL、目标 role 权限和 migration 状态") from None
    finally:
        try:
            await engine.dispose()
        except Exception:
            raise PostgresSetupError("PostgreSQL engine 清理失败；请确认没有其他初始化任务仍在运行") from None


async def setup_postgres(
    admin_url: str,
    database_url: str,
    *,
    expected_database: str | None = None,
) -> SetupResult:
    """幂等创建目标数据库，并用现有 bootstrap/Alembic 升级到 head。"""
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
    revision = await _bootstrap_existing(database_url)
    return SetupResult(
        host=target.host,
        port=target.port,
        database=target.database,
        owner=target.username,
        created=created,
        revision=revision,
    )


async def migrate_postgres(database_url: str, *, expected_database: str | None = None) -> SetupResult:
    """Upgrade an existing database without using administrator credentials."""
    target = parse_target(database_url)
    if expected_database is not None:
        expected_database = validate_identifier(expected_database, kind="database")
        if target.database != expected_database:
            raise ValueError("DATABASE_URL database does not match --database")
    revision = await _bootstrap_existing(database_url)
    return SetupResult(
        host=target.host,
        port=target.port,
        database=target.database,
        owner=target.username,
        created=False,
        revision=revision,
    )


def print_result(result: SetupResult, *, migrate_only: bool = False) -> None:
    action = "已升级" if migrate_only else ("已创建并初始化" if result.created else "已存在并完成初始化")
    print(f"PostgreSQL 数据库{action}")
    print(f"主机: {result.host}:{result.port}")
    print(f"数据库: {result.database}")
    print(f"Alembic revision: {result.revision}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="初始化或升级 DeerFlow PostgreSQL 数据库")
    parser.add_argument("--database", help="必须与 DATABASE_URL 中的数据库名一致")
    parser.add_argument(
        "--migrate-only",
        action="store_true",
        help="仅升级已存在数据库，不读取 POSTGRES_ADMIN_URL，也不创建数据库",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须显式设置 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        if args.migrate_only:
            result = asyncio.run(migrate_postgres(database_url, expected_database=args.database))
        else:
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
        print_result(result, migrate_only=args.migrate_only)
        return 0
    except (PostgresSetupError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
