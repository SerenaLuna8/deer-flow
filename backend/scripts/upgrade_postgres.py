"""显式升级 ActWeave PostgreSQL 数据库到迁移链头（唯一升级入口，见 D3）。

Gateway/Worker/Scheduler 永不自动迁移：behind 库在运行时只会 fail-closed 并指向
本脚本。升级流程为 分类 → 备份确认 → 会话级 advisory lock → ``alembic upgrade
head`` → 升级后重算 catalog digest 校验（必须与全新安装完全一致）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import (
    CURRENT_SCHEMA_REVISION,
    SCHEMA_MUTATION_LOCK_KEY,
    M7RecreateRequired,
    classify_database,
)

try:
    from scripts.setup_postgres import parse_target
except ModuleNotFoundError:  # Direct ``python scripts/upgrade_postgres.py`` execution.
    from setup_postgres import parse_target

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_PATH = BACKEND_ROOT / "migrations"

# The production bootstrap path used by `make setup-db` takes this same lock.
# Sharing it prevents setup and an operator-driven upgrade from mutating one
# target schema concurrently.
_UPGRADE_LOCK_KEY = SCHEMA_MUTATION_LOCK_KEY
_LOCK_POLL_SECONDS = 0.1


class PostgresUpgradeError(RuntimeError):
    """A credential-safe PostgreSQL upgrade failure."""


@dataclass(frozen=True)
class UpgradeResult:
    host: str
    port: int
    database: str
    from_revision: str
    to_revision: str
    applied: bool


def _run_alembic_upgrade_sync(database_url: str) -> None:
    """Run the chain synchronously; called via ``asyncio.to_thread``."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    config.attributes["sqlalchemy_url"] = database_url
    command.upgrade(config, "head")


async def upgrade_postgres(database_url: str, *, assume_yes: bool = False) -> UpgradeResult:
    """升级 behind 库到链头；current 库为幂等空操作。"""
    target = parse_target(database_url)
    engine = create_async_engine(
        DatabaseConfig(url=database_url).sqlalchemy_url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with engine.connect() as lock_connection:
            await lock_connection.execute(text("SET statement_timeout = 0"))
            await lock_connection.execute(text("SET idle_in_transaction_session_timeout = 0"))
            while not await lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _UPGRADE_LOCK_KEY},
            ):
                await asyncio.sleep(_LOCK_POLL_SECONDS)
            try:
                return await _upgrade_locked(
                    engine,
                    database_url,
                    target_host=target.host,
                    target_port=target.port,
                    target_database=target.database,
                    assume_yes=assume_yes,
                )
            finally:
                try:
                    await lock_connection.scalar(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": _UPGRADE_LOCK_KEY},
                    )
                except Exception:
                    # Closing the dedicated session releases the session lock.
                    pass
    finally:
        await engine.dispose()


async def _upgrade_locked(
    engine,
    database_url: str,
    *,
    target_host: str,
    target_port: int,
    target_database: str,
    assume_yes: bool,
) -> UpgradeResult:
    async with engine.connect() as connection:
        try:
            state = await classify_database(connection)
        except M7RecreateRequired:
            raise PostgresUpgradeError(f"M7_RECREATE_REQUIRED: 目标库的 marker 未知或 catalog 已漂移，不在受支持的迁移链上；请人工检查后显式重建（当前链头 {CURRENT_SCHEMA_REVISION}）") from None
        if state == "empty":
            raise PostgresUpgradeError("目标库为空；升级只服务存量库，请运行 `make setup-db` 全新安装")
        current_marker = str(await connection.scalar(text("SELECT version_num FROM alembic_version")))

    if state == "current":
        return UpgradeResult(
            host=target_host,
            port=target_port,
            database=target_database,
            from_revision=current_marker,
            to_revision=CURRENT_SCHEMA_REVISION,
            applied=False,
        )

    if not assume_yes and not _confirm_interactively(current_marker):
        raise PostgresUpgradeError('升级已取消；请先完成数据库备份，再运行 `make upgrade-db`（或传入 ARGS="--yes" 跳过确认）')

    await asyncio.to_thread(_run_alembic_upgrade_sync, database_url)

    async with engine.connect() as connection:
        try:
            post_state = await classify_database(connection)
        except M7RecreateRequired:
            post_state = "drifted"
        if post_state != "current":
            raise PostgresUpgradeError("升级后校验失败：catalog 与全新安装不一致；请从升级前备份恢复并报告该问题")
    return UpgradeResult(
        host=target_host,
        port=target_port,
        database=target_database,
        from_revision=current_marker,
        to_revision=CURRENT_SCHEMA_REVISION,
        applied=True,
    )


def _confirm_interactively(current_marker: str) -> bool:
    if not sys.stdin.isatty():
        return False
    print(f"目标库 marker: {current_marker}，将升级到链头: {CURRENT_SCHEMA_REVISION}")
    print("升级不支持 downgrade；继续前必须已完成数据库备份（例如 pg_dump）。")
    answer = input("已完成备份并确认升级？输入 yes 继续: ")
    return answer.strip().lower() == "yes"


def print_result(result: UpgradeResult) -> None:
    if result.applied:
        print(f"PostgreSQL 数据库已升级: {result.from_revision} -> {result.to_revision}")
    else:
        print(f"PostgreSQL 数据库已在链头 {result.to_revision}，无需升级")
    print(f"主机: {result.host}:{result.port}")
    print(f"数据库: {result.database}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="显式升级 ActWeave PostgreSQL 数据库到迁移链头")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认（自动化场景；调用方自行保证已备份）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须显式设置 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(upgrade_postgres(database_url, assume_yes=args.yes))
    except (PostgresUpgradeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
