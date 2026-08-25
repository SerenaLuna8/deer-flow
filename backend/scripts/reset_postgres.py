"""显式重置 ActWeave PostgreSQL Schema V1。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.system_settings.bootstrap import (
    DefaultSystemModelBootstrapConfigurationInvalid,
    prepare_default_system_model_bootstrap,
)
from deerflow.config.database_config import DatabaseConfig
from scripts.setup_postgres import (
    SetupResult,
    parse_target,
)
from scripts.setup_postgres import (
    _bootstrap_empty_schema_under_lock as bootstrap_empty_schema_under_lock,
)
from scripts.setup_postgres import (
    _complete_bootstrap_lock as complete_bootstrap_lock,
)


class PostgresResetError(RuntimeError):
    """A sanitized operator-facing reset failure."""


_PROTECTED_DATABASES = frozenset({"postgres", "template0", "template1"})


def _require_resettable_database(database: str) -> None:
    if database.casefold() in _PROTECTED_DATABASES:
        raise PostgresResetError("拒绝重置 PostgreSQL 受保护数据库")


async def reset_and_initialize(
    database_url: str,
    *,
    expected_database: str,
) -> SetupResult:
    target = parse_target(database_url)
    _require_resettable_database(target.database)
    if target.database != expected_database:
        raise PostgresResetError("确认数据库名与 DATABASE_URL 不一致")

    try:
        default_model_bootstrap = prepare_default_system_model_bootstrap()
    except DefaultSystemModelBootstrapConfigurationInvalid:
        raise PostgresResetError("数据库重置预检失败；请检查 bootstrap Secret 和模型 Key 配置") from None
    except Exception:
        raise PostgresResetError("数据库重置预检失败；请检查 bootstrap Secret 和模型 Key 配置") from None

    try:
        engine = create_async_engine(
            DatabaseConfig(url=database_url).sqlalchemy_url,
            poolclass=NullPool,
        )
    except Exception:
        raise PostgresResetError("数据库重置预检失败；无法创建目标数据库连接") from None
    destructive_statement_started = False
    schema_rebuilt = False
    bootstrap_completed = False
    try:
        async with complete_bootstrap_lock(database_url):
            async with engine.begin() as connection:
                current_database = await connection.scalar(text("SELECT current_database()"))
                if current_database != target.database:
                    raise PostgresResetError("数据库重置预检失败：实际连接目标与 DATABASE_URL 不一致")
                current_schema = await connection.scalar(text("SELECT current_schema()"))
                if current_schema != "public":
                    raise PostgresResetError("数据库重置预检失败：有效 search_path 必须以 public 为当前 Schema")
                destructive_statement_started = True
                await connection.execute(text("DROP SCHEMA public CASCADE"))
                await connection.execute(text("CREATE SCHEMA public AUTHORIZATION CURRENT_USER"))
            schema_rebuilt = True
            revision = await bootstrap_empty_schema_under_lock(
                database_url,
                default_model_bootstrap=default_model_bootstrap,
                force_public_schema=True,
            )
            bootstrap_completed = True
    except PostgresResetError:
        raise
    except Exception:
        if bootstrap_completed:
            raise PostgresResetError(
                "Schema V1 初始化已完成，但重置协调清理失败；请先运行 `make check-db` 确认状态",
            ) from None
        if schema_rebuilt:
            raise PostgresResetError(
                "数据库 Schema 已重建；Schema V1 初始化结果未知，可能完整或仅部分完成；请先运行 `make check-db` 确认状态后再决定是否重试",
            ) from None
        if destructive_statement_started:
            raise PostgresResetError(
                "数据库 Schema 重置提交结果未知；目标库可能已重建；请先运行 `make check-db` 确认状态后再决定是否重试",
            ) from None
        raise PostgresResetError("PostgreSQL Schema 重置在删除前失败；原 Schema 未执行删除，请检查目标、权限和活动事务") from None
    finally:
        try:
            await engine.dispose()
        except Exception:
            pass
    return SetupResult(
        host=target.host,
        port=target.port,
        database=target.database,
        owner=target.username,
        created=False,
        revision=revision,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="重置 ActWeave PostgreSQL 数据库")
    parser.add_argument(
        "--confirm-database",
        default=os.getenv("CONFIRM_DATABASE"),
        help="必须与 DATABASE_URL 中的数据库名完全一致",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须显式设置 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        target = parse_target(database_url)
        _require_resettable_database(target.database)
    except (PostgresResetError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    print(
        f"即将永久清空数据库: {target.host}:{target.port}/{target.database}",
        flush=True,
    )
    confirmation = args.confirm_database
    if not confirmation and not sys.stdin.isatty():
        print(
            f"错误: 非交互执行必须设置 CONFIRM_DATABASE={target.database}",
            file=sys.stderr,
        )
        return 2
    if not confirmation:
        confirmation = input(f"输入数据库名 {target.database} 以确认永久删除全部数据: ").strip()
    if confirmation != target.database:
        print("错误: 确认数据库名与 DATABASE_URL 不一致", file=sys.stderr)
        return 2

    try:
        result = asyncio.run(
            reset_and_initialize(
                database_url,
                expected_database=target.database,
            )
        )
    except PostgresResetError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print("PostgreSQL 数据库已重置并初始化")
    print(f"目标: {result.host}:{result.port}/{result.database}")
    print(f"Schema marker: {result.revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
