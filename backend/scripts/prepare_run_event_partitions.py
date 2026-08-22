"""Explicit operator entry point for proactive ``run_events`` partitions.

The application runtime remains a read-only schema consumer. Operators should
schedule this idempotent command so the current UTC month and the following two
months exist before request traffic reaches a month boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import SchemaRecreateRequired, classify_database

try:
    from scripts.setup_postgres import parse_target
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from setup_postgres import parse_target


class RunEventPartitionPreparationError(RuntimeError):
    """A credential-safe refusal to prepare RunEvent partitions."""


@dataclass(frozen=True, slots=True)
class RunEventPartitionPreparationResult:
    host: str
    port: int
    database: str
    month_starts: tuple[datetime, ...]
    partitions: tuple[str, ...]


def partition_month_starts(as_of: datetime) -> tuple[datetime, datetime, datetime]:
    """Return UTC month starts for N, N+1, and N+2."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise RunEventPartitionPreparationError("as_of must be timezone-aware")
    utc_as_of = as_of.astimezone(UTC)
    first_month_index = utc_as_of.year * 12 + utc_as_of.month - 1
    starts: list[datetime] = []
    for offset in range(3):
        month_index = first_month_index + offset
        year, zero_based_month = divmod(month_index, 12)
        starts.append(datetime(year, zero_based_month + 1, 1, tzinfo=UTC))
    return starts[0], starts[1], starts[2]


async def prepare_run_event_partitions(
    database_url: str,
    *,
    as_of: datetime | None = None,
) -> RunEventPartitionPreparationResult:
    """Create N through N+2 partitions through the installed schema function."""

    month_starts = partition_month_starts(as_of or datetime.now(UTC))
    target = parse_target(database_url)
    engine = create_async_engine(
        DatabaseConfig(url=database_url).sqlalchemy_url,
        poolclass=NullPool,
    )
    try:
        async with engine.begin() as connection:
            try:
                state = await classify_database(connection)
            except SchemaRecreateRequired:
                raise RunEventPartitionPreparationError(
                    "目标库 schema 未处于可验证状态；请先运行 make check-db",
                ) from None
            if state != "current":
                raise RunEventPartitionPreparationError(
                    "目标库不在当前 Schema V1；请先运行 make check-db",
                )
            # A queued ACCESS EXCLUSIVE request can otherwise stall ordinary
            # traffic behind it. Operators can safely retry this idempotent job.
            await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            partitions: list[str] = []
            for month_start in month_starts:
                partition = await connection.scalar(
                    text("SELECT ensure_run_events_month_partition(:month_start)"),
                    {"month_start": month_start},
                )
                expected = f"run_events_p{month_start:%Y%m}"
                if partition != expected:
                    raise RunEventPartitionPreparationError(
                        "数据库返回了非预期的 run_events 分区名；事务已回滚",
                    )
                partitions.append(partition)
        return RunEventPartitionPreparationResult(
            host=target.host,
            port=target.port,
            database=target.database,
            month_starts=month_starts,
            partitions=tuple(partitions),
        )
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="幂等预创建 run_events 当前 UTC 月至 N+2 月分区",
    )


def print_result(result: RunEventPartitionPreparationResult) -> None:
    print("run_events 分区预创建：已完成")
    print(f"目标: {result.host}:{result.port}/{result.database}")
    print(f"覆盖: {result.month_starts[0]:%Y-%m} 至 {result.month_starts[-1]:%Y-%m} (UTC)")
    print(f"已确认分区: {', '.join(result.partitions)}")


def main(argv: list[str] | None = None) -> int:
    _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须通过根 .env 或显式环境提供 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(prepare_run_event_partitions(database_url))
    except (RunEventPartitionPreparationError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("错误: 分区预创建数据库操作失败；请检查数据库日志后重试", file=sys.stderr)
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
