"""Explicitly apply packaged System Asset releases to a current database."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.shared_assets.bootstrap import (
    BootstrapCatalogError,
    BootstrapConflict,
    BootstrapResult,
    bootstrap_system_assets,
)
from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import (
    SCHEMA_MUTATION_LOCK_KEY,
    M7RecreateRequired,
    classify_database,
)


class SystemAssetUpgradeError(RuntimeError):
    """A credential-safe packaged System Asset upgrade failure."""


async def upgrade_system_assets(database_url: str) -> BootstrapResult:
    """Apply new immutable releases while schema mutation is excluded."""

    lock_engine = None
    mutation_engine = None
    try:
        sqlalchemy_url = DatabaseConfig(url=database_url).sqlalchemy_url
        lock_engine = create_async_engine(
            sqlalchemy_url,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        mutation_engine = create_async_engine(
            sqlalchemy_url,
            poolclass=NullPool,
        )
    except Exception:
        for engine in (mutation_engine, lock_engine):
            if engine is None:
                continue
            try:
                await engine.dispose()
            except Exception:
                pass
        raise SystemAssetUpgradeError("DATABASE_URL 不是可用的 PostgreSQL 连接配置") from None

    primary_error: SystemAssetUpgradeError | None = None
    try:
        async with lock_engine.connect() as lock_connection:
            lock_acquired = False
            try:
                await lock_connection.execute(text("SET statement_timeout = 0"))
                await lock_connection.execute(text("SET idle_in_transaction_session_timeout = 0"))
                idle_session_timeout = await lock_connection.scalar(text("SELECT current_setting('idle_session_timeout', true)"))
                if idle_session_timeout is not None:
                    await lock_connection.execute(text("SET idle_session_timeout = 0"))
                await lock_connection.execute(
                    text("SELECT pg_advisory_lock(:lock_key)"),
                    {"lock_key": SCHEMA_MUTATION_LOCK_KEY},
                )
                lock_acquired = True
                try:
                    state = await classify_database(lock_connection)
                except M7RecreateRequired:
                    raise SystemAssetUpgradeError("M7_RECREATE_REQUIRED: 目标库 marker 未知或 schema catalog 已漂移；请人工检查后显式重建") from None
                if state == "empty":
                    raise SystemAssetUpgradeError("目标库尚未初始化；请先运行 `make setup-db`")
                if state == "behind":
                    raise SystemAssetUpgradeError("目标库 schema 不是当前链头；请先备份并运行 `make upgrade-db`")
                if state != "current":
                    raise SystemAssetUpgradeError("目标库 schema 状态不受支持；未应用任何 System Asset release")

                try:
                    return await bootstrap_system_assets(
                        async_sessionmaker(
                            mutation_engine,
                            expire_on_commit=False,
                        )
                    )
                except BootstrapConflict:
                    raise SystemAssetUpgradeError("现有 System Asset 与打包发布历史冲突；未应用任何 release") from None
                except BootstrapCatalogError:
                    raise SystemAssetUpgradeError("打包 System Asset catalog 无效；未修改数据库") from None
                except SQLAlchemyError:
                    raise SystemAssetUpgradeError("System Asset release 应用结果不确定；操作幂等，请检查数据库后安全重跑") from None
                except Exception:
                    raise SystemAssetUpgradeError("System Asset release 应用结果不确定；操作幂等，请检查数据库后安全重跑") from None
            except SystemAssetUpgradeError as error:
                primary_error = error
                raise
            except SQLAlchemyError:
                primary_error = SystemAssetUpgradeError("无法验证或更新 System Asset；请检查数据库状态和访问权限")
                raise primary_error from None
            except Exception:
                primary_error = SystemAssetUpgradeError("无法验证或更新 System Asset；未修改数据库")
                raise primary_error from None
            finally:
                if lock_acquired:
                    try:
                        released = await lock_connection.scalar(
                            text("SELECT pg_advisory_unlock(:lock_key)"),
                            {"lock_key": SCHEMA_MUTATION_LOCK_KEY},
                        )
                        if released is not True:
                            if primary_error is None:
                                raise SystemAssetUpgradeError("System Asset 操作的最终状态无法确认（结果不确定）；操作幂等，请检查数据库后安全重跑")
                    except SystemAssetUpgradeError:
                        raise
                    except Exception:
                        if primary_error is None:
                            raise SystemAssetUpgradeError("System Asset 操作的最终状态无法确认（结果不确定）；操作幂等，请检查数据库后安全重跑") from None
    except SystemAssetUpgradeError:
        raise
    except Exception:
        if primary_error is not None:
            raise primary_error from None
        raise SystemAssetUpgradeError("System Asset 操作的最终状态无法确认（结果不确定）；操作幂等，请检查数据库后安全重跑") from None
    finally:
        for engine in (mutation_engine, lock_engine):
            if engine is None:
                continue
            try:
                await engine.dispose()
            except Exception:
                pass


def print_result(result: BootstrapResult) -> None:
    """Print only non-sensitive catalog and release counts."""

    print("System Asset catalog 已完成校验与应用")
    print(f"本次新增不可变 release: {result.applied_releases}")
    print(f"打包资产: Agent {result.counts['agent']} / Skill {result.counts['skill']} / MCP {result.counts['mcp']}")
    print(f"Catalog SHA-256: {result.digest}")


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=("显式应用打包 System Asset 的新增不可变 release；请在停止 Gateway/Worker/Scheduler 的维护窗口执行"))


def main(argv: list[str] | None = None) -> int:
    _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须显式设置 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(upgrade_system_assets(database_url))
    except SystemAssetUpgradeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
