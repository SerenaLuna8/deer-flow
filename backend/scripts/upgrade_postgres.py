"""Explicitly upgrade PostgreSQL through the packaged forward-only schema chain."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.schema_upgrade import (
    SchemaUpgradeError,
    upgrade_schema,
    validate_schema_upgrade_artifacts,
)
from scripts.setup_postgres import parse_target


class PostgresUpgradeError(RuntimeError):
    """A credential-safe operator-facing schema upgrade failure."""


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    host: str
    port: int
    database: str
    previous_revision: str
    current_revision: str
    upgraded: bool


async def upgrade_postgres(database_url: str) -> UpgradeResult:
    """Upgrade one explicit PostgreSQL target without bootstrap credentials."""

    target = parse_target(database_url)
    try:
        validate_schema_upgrade_artifacts()
    except Exception:
        raise PostgresUpgradeError(
            "数据库升级产物预检失败；未访问目标数据库",
        ) from None

    try:
        engine = create_async_engine(
            DatabaseConfig(url=database_url).sqlalchemy_url,
            poolclass=NullPool,
        )
    except Exception:
        raise PostgresUpgradeError("DATABASE_URL 不是可用的 PostgreSQL 连接配置") from None

    try:
        outcome = await upgrade_schema(engine)
    except SchemaUpgradeError as exc:
        raise PostgresUpgradeError(str(exc)) from None
    except Exception:
        raise PostgresUpgradeError(
            "数据库升级失败或提交结果未知；请运行 `make check-db` 核对状态",
        ) from None
    finally:
        try:
            await engine.dispose()
        except Exception:
            pass

    return UpgradeResult(
        host=target.host,
        port=target.port,
        database=target.database,
        previous_revision=outcome.previous_revision,
        current_revision=outcome.current_revision,
        upgraded=outcome.upgraded,
    )


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="升级 ActWeave PostgreSQL 数据库到当前 Schema 版本",
    )


def main(argv: list[str] | None = None) -> int:
    _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("错误: 必须显式设置 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(upgrade_postgres(database_url))
    except (PostgresUpgradeError, ValueError):
        print(
            "错误: 数据库升级未完成；请运行 `make check-db` 查看脱敏状态",
            file=sys.stderr,
        )
        return 1

    target = f"{result.host}:{result.port}/{result.database}"
    if result.upgraded:
        print(
            f"PostgreSQL Schema 已从 {result.previous_revision} 升级到 {result.current_revision}",
        )
    else:
        print(f"PostgreSQL Schema 已是当前版本 {result.current_revision}")
    print(f"目标: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
