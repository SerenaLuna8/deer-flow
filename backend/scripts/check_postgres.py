"""只读检查 DeerFlow PostgreSQL 连接与 schema 健康状态。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Literal

import asyncpg

from deerflow.persistence.bootstrap import _get_head_revision

try:
    from scripts.setup_postgres import _asyncpg_url, parse_target
except ModuleNotFoundError:  # Direct ``python scripts/check_postgres.py`` execution.
    from setup_postgres import _asyncpg_url, parse_target

REQUIRED_TABLES: tuple[str, ...] = (
    "audit_logs",
    "automation_cutover_state",
    "automation_migration_ledger",
    "automation_migration_runs",
    "channel_connections",
    "channel_conversations",
    "channel_credentials",
    "channel_oauth_states",
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
    "feedback",
    "jobs",
    "job_attempts",
    "dead_jobs",
    "deletion_tombstones",
    "migration_ledger",
    "project_memberships",
    "project_quotas",
    "project_usage_counters",
    "project_usage_ledger",
    "projects",
    "run_events",
    "runs",
    "scheduled_task_runs",
    "scheduled_tasks",
    "recovery_journal_state",
    "reliability_cutover_state",
    "reliability_migration_ledger",
    "reliability_migration_runs",
    "restore_proofs",
    "store",
    "store_migrations",
    "threads_meta",
    "thread_event_sequences",
    "users",
    "worker_nodes",
)

_UNDEFINED_TABLE_SQLSTATE = "42P01"


@dataclass(frozen=True)
class PostgresCheckResult:
    host: str
    port: int
    database: str
    server_version: str | None = None
    current_revision: str | None = None
    head_revision: str | None = None
    revision_matches: bool = False
    missing_tables: tuple[str, ...] = ()
    connected: bool = True
    error: str = ""
    automation_status: Literal["ready", "migration_required", "unavailable"] = "unavailable"
    reliability_status: Literal["ready", "migration_required", "unavailable"] = "unavailable"

    @property
    def healthy(self) -> bool:
        return self.connected and self.revision_matches and not self.missing_tables and not self.error and self.automation_status == "ready" and self.reliability_status == "ready"


def get_head_revision() -> str:
    return _get_head_revision()


async def check_postgres(database_url: str) -> PostgresCheckResult:
    """只读检查连接、PostgreSQL 版本、current/head revision 与必需表。"""
    target = parse_target(database_url)
    head = get_head_revision()
    connection = None
    try:
        connection = await asyncpg.connect(_asyncpg_url(database_url))
        server_version = await connection.fetchval("SELECT version()")
        try:
            current_revision = await connection.fetchval("SELECT version_num FROM alembic_version")
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != _UNDEFINED_TABLE_SQLSTATE:
                raise
            current_revision = None
        rows = await connection.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
            list(REQUIRED_TABLES),
        )
        present = {row["table_name"] for row in rows}
        missing_tables = tuple(sorted(set(REQUIRED_TABLES) - present))
        automation_status: Literal["ready", "migration_required", "unavailable"] = "migration_required"
        if (
            not {
                "automation_cutover_state",
                "automation_migration_ledger",
                "automation_migration_runs",
            }
            - present
        ):
            marker_ready = await connection.fetchval(
                """SELECT EXISTS (
                       SELECT 1 FROM automation_cutover_state
                       WHERE id = 1
                         AND stage = 'cutover_complete'
                         AND final_schema_probe_complete = true
                         AND cutover_at IS NOT NULL
                   )"""
            )
            # A completed M5 marker remains authoritative while the database is
            # intentionally parked at 0013 for the explicit M6 cutover.
            if marker_ready:
                automation_status = "ready"
        reliability_status: Literal["ready", "migration_required", "unavailable"] = "migration_required"
        if (
            not {
                "reliability_cutover_state",
                "reliability_migration_ledger",
                "reliability_migration_runs",
            }
            - present
        ):
            reliability_ready = await connection.fetchval(
                """SELECT EXISTS (
                       SELECT 1 FROM reliability_cutover_state
                       WHERE id=1 AND stage='cutover_complete'
                         AND final_schema_probe_complete=true
                         AND schema_revision='0015_project_reliability_finalize'
                         AND cutover_at IS NOT NULL
                   )"""
            )
            if current_revision == head and reliability_ready:
                reliability_status = "ready"
        return PostgresCheckResult(
            host=target.host,
            port=target.port,
            database=target.database,
            server_version=str(server_version),
            current_revision=current_revision,
            head_revision=head,
            revision_matches=current_revision == head,
            missing_tables=missing_tables,
            automation_status=automation_status,
            reliability_status=reliability_status,
        )
    except Exception:
        return PostgresCheckResult(
            host=target.host,
            port=target.port,
            database=target.database,
            head_revision=head,
            connected=False,
            error="无法连接或读取 PostgreSQL；请检查 DATABASE_URL、数据库状态和访问权限",
        )
    finally:
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass


def run_check(database_url: str) -> PostgresCheckResult:
    return asyncio.run(check_postgres(database_url))


def print_result(result: PostgresCheckResult) -> None:
    status = "健康" if result.healthy else "不健康"
    print(f"PostgreSQL 状态: {status}")
    print(f"主机: {result.host}:{result.port}")
    print(f"数据库: {result.database}")
    if result.server_version:
        print(f"版本: {result.server_version}")
    print(f"当前 Alembic revision: {result.current_revision or '缺失'}")
    print(f"目标 Alembic revision: {result.head_revision or '未知'}")
    print(f"Automation: {result.automation_status}")
    print(f"Reliability: {result.reliability_status}")
    if result.missing_tables:
        print(f"缺失表: {', '.join(result.missing_tables)}")
    if result.error:
        print(f"错误: {result.error}")


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="只读检查 DeerFlow PostgreSQL 数据库").parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须显式设置 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        result = run_check(database_url)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print_result(result)
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
