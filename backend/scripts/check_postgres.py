"""只读检查 DeerFlow PostgreSQL 连接与 schema 健康状态。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

import asyncpg

from deerflow.persistence.bootstrap import _get_head_revision

try:
    from scripts.setup_postgres import _asyncpg_url, parse_target
except ModuleNotFoundError:  # Direct ``python scripts/check_postgres.py`` execution.
    from setup_postgres import _asyncpg_url, parse_target

REQUIRED_TABLES: tuple[str, ...] = (
    "agent_version_mcp_refs",
    "agent_version_skill_refs",
    "agent_versions",
    "agents",
    "artifacts",
    "asset_catalog_state",
    "audit_logs",
    "channel_connections",
    "channel_conversations",
    "channel_credentials",
    "channel_oauth_states",
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
    "credential_envelopes",
    "credential_grants",
    "credential_versions",
    "credentials",
    "dead_jobs",
    "deletion_tombstones",
    "feedback",
    "file_chunks",
    "files",
    "job_attempts",
    "jobs",
    "mcp_server_versions",
    "mcp_servers",
    "mcp_version_credential_slots",
    "project_invitation_rate_limits",
    "project_invitations",
    "project_memberships",
    "project_quotas",
    "project_system_agent_bindings",
    "project_system_mcp_bindings",
    "project_system_skill_bindings",
    "project_usage_counters",
    "project_usage_ledger",
    "projects",
    "recovery_journal_state",
    "restore_proofs",
    "run_asset_versions",
    "run_events",
    "run_mcp_grant_snapshots",
    "runs",
    "scheduled_task_runs",
    "scheduled_tasks",
    "skill_version_files",
    "skill_versions",
    "skills",
    "store",
    "store_migrations",
    "thread_event_sequences",
    "threads_meta",
    "user_project_memories",
    "user_project_memory_facts",
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

    @property
    def healthy(self) -> bool:
        return self.connected and self.revision_matches and not self.missing_tables and not self.error


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
        return PostgresCheckResult(
            host=target.host,
            port=target.port,
            database=target.database,
            server_version=str(server_version),
            current_revision=current_revision,
            head_revision=head,
            revision_matches=current_revision == head,
            missing_tables=missing_tables,
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
