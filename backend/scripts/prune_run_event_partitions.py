"""Explicit global age-retention entry point for monthly ``run_events`` data.

Project/account/owner privacy deletion must continue to delete exact-scope rows.
This command is only for a global UTC month cutoff, where dropping a whole
partition cannot remove data outside the requested retention boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import SchemaRecreateRequired, classify_database

try:
    from scripts.setup_postgres import parse_target
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from setup_postgres import parse_target

_PARTITION_NAME = re.compile(r"^run_events_p(?P<year>[0-9]{4})(?P<month>0[1-9]|1[0-2])$")


class RunEventRetentionError(RuntimeError):
    """A credential-safe refusal to run global event retention."""


@dataclass(frozen=True, slots=True)
class RunEventRetentionResult:
    host: str
    port: int
    database: str
    cutoff: datetime
    eligible_partitions: tuple[str, ...]
    dropped_partitions: int
    applied: bool


def parse_utc_month_start(value: str) -> datetime:
    """Accept only an explicit UTC calendar-month boundary."""

    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        cutoff = datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        raise RunEventRetentionError(
            "--before 必须是 UTC 月初，例如 2026-08-01T00:00:00Z",
        ) from None
    if cutoff.tzinfo is None or cutoff.utcoffset() != UTC.utcoffset(cutoff):
        raise RunEventRetentionError("--before 必须显式使用 UTC 时区")
    cutoff = cutoff.astimezone(UTC)
    if cutoff.day != 1 or any((cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond)):
        raise RunEventRetentionError("--before 必须精确落在 UTC 月初 00:00:00")
    if cutoff > datetime.now(UTC):
        raise RunEventRetentionError("--before 不能位于未来")
    return cutoff


def _partition_month_start(name: str) -> datetime | None:
    match = _PARTITION_NAME.fullmatch(name)
    if match is None:
        return None
    return datetime(
        int(match.group("year")),
        int(match.group("month")),
        1,
        tzinfo=UTC,
    )


async def _eligible_partitions(connection, cutoff: datetime) -> tuple[str, ...]:
    rows = await connection.execute(
        text(
            """SELECT child.relname
            FROM pg_inherits AS inheritance
            JOIN pg_class AS parent ON parent.oid = inheritance.inhparent
            JOIN pg_class AS child ON child.oid = inheritance.inhrelid
            JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace
            WHERE parent.oid = 'run_events'::regclass
              AND namespace.nspname = current_schema()
            ORDER BY child.relname"""
        )
    )
    eligible: list[str] = []
    for name in rows.scalars():
        month_start = _partition_month_start(str(name))
        if month_start is None:
            raise RunEventRetentionError(
                "run_events 存在无法识别的子分区；拒绝执行 DROP",
            )
        if month_start < cutoff:
            eligible.append(str(name))
    return tuple(eligible)


async def prune_run_event_partitions(
    database_url: str,
    cutoff: datetime,
    *,
    apply: bool,
) -> RunEventRetentionResult:
    """Preview or apply one global UTC-month partition retention cutoff."""

    if cutoff.tzinfo is None or cutoff.utcoffset() != UTC.utcoffset(cutoff):
        raise RunEventRetentionError("cutoff must be timezone-aware UTC")
    cutoff = cutoff.astimezone(UTC)
    if cutoff.day != 1 or any((cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond)):
        raise RunEventRetentionError("cutoff must be an exact UTC month boundary")
    if cutoff > datetime.now(UTC):
        raise RunEventRetentionError("cutoff cannot be in the future")
    target = parse_target(database_url)
    engine = create_async_engine(
        DatabaseConfig(url=database_url).sqlalchemy_url,
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            try:
                state = await classify_database(connection)
            except SchemaRecreateRequired:
                raise RunEventRetentionError(
                    "目标库 schema 未处于可验证状态；请先运行 make check-db",
                ) from None
            if state != "current":
                raise RunEventRetentionError(
                    "目标库不在当前 Schema V1；请先运行 make check-db",
                )
            eligible = await _eligible_partitions(connection, cutoff)
        dropped = 0
        if apply:
            async with engine.begin() as connection:
                dropped = int(
                    await connection.scalar(
                        text("SELECT drop_run_event_partitions_before(:cutoff)"),
                        {"cutoff": cutoff},
                    )
                    or 0
                )
        return RunEventRetentionResult(
            host=target.host,
            port=target.port,
            database=target.database,
            cutoff=cutoff,
            eligible_partitions=eligible,
            dropped_partitions=dropped,
            applied=apply,
        )
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="预览或执行 run_events 全局 UTC 月分区保留清理",
    )
    parser.add_argument(
        "--before",
        required=True,
        help="保留边界，必须是 UTC 月初，例如 2026-08-01T00:00:00Z",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="实际 DROP；省略时只读预览",
    )
    return parser


def print_result(result: RunEventRetentionResult) -> None:
    mode = "已执行" if result.applied else "只读预览"
    print(f"run_events 分区保留：{mode}")
    print(f"目标: {result.host}:{result.port}/{result.database}")
    print(f"UTC cutoff: {result.cutoff.isoformat().replace('+00:00', 'Z')}")
    print(f"符合 DROP 条件的分区: {len(result.eligible_partitions)}")
    if result.applied:
        print(f"实际 DROP 分区: {result.dropped_partitions}")
    elif result.eligible_partitions:
        print("确认备份和全局保留边界后，追加 --yes 执行")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须通过根 .env 或显式环境提供 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        cutoff = parse_utc_month_start(args.before)
        result = asyncio.run(
            prune_run_event_partitions(
                database_url,
                cutoff,
                apply=args.yes,
            )
        )
    except (RunEventRetentionError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
