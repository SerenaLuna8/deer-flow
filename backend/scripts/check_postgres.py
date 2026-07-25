"""只读检查 DeerFlow PostgreSQL 连接与 schema 健康状态。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import M7RecreateRequired, _get_head_revision, classify_database

try:
    from scripts.setup_postgres import parse_target
except ModuleNotFoundError:  # Direct ``python scripts/check_postgres.py`` execution.
    from setup_postgres import parse_target

REQUIRED_TABLES: tuple[str, ...] = (
    "agent_version_mcp_refs",
    "agent_version_skill_refs",
    "agent_versions",
    "agents",
    "artifacts",
    "asset_catalog_state",
    "audit_logs",
    "auth_sessions",
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
    "user_notifications",
    "users",
    "worker_nodes",
)


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
    schema_state: Literal[
        "ready",
        "migration_required",
        "uninitialized",
        "recreate_required",
        "unavailable",
    ] = "unavailable"
    connected: bool = True
    error: str = ""

    @property
    def healthy(self) -> bool:
        return self.connected and self.schema_state == "ready" and self.revision_matches and not self.missing_tables and not self.error


def get_head_revision() -> str:
    return _get_head_revision()


async def check_postgres(database_url: str) -> PostgresCheckResult:
    """只读检查连接、PostgreSQL 版本、current/head revision 与必需表。"""
    target = parse_target(database_url)
    head = get_head_revision()
    engine = create_async_engine(
        DatabaseConfig(url=database_url).sqlalchemy_url,
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            server_version = await connection.scalar(text("SELECT version()"))
            has_revision_table = await connection.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL"))
            current_revision = await connection.scalar(text("SELECT version_num FROM alembic_version")) if has_revision_table else None
            rows = await connection.execute(
                text(
                    """SELECT table_name FROM information_schema.tables
                    WHERE table_schema=current_schema()
                      AND table_name=ANY(CAST(:required_tables AS text[]))"""
                ),
                {"required_tables": list(REQUIRED_TABLES)},
            )
            present = set(rows.scalars())
            missing_tables = tuple(sorted(set(REQUIRED_TABLES) - present))
            try:
                database_state = await classify_database(connection)
            except M7RecreateRequired:
                schema_state = "recreate_required"
                error = "M7_RECREATE_REQUIRED: revision 未知或 schema catalog 发生漂移，需要人工检查"
            else:
                if database_state == "current":
                    schema_state = "ready"
                    error = ""
                elif database_state == "upgradeable":
                    schema_state = "migration_required"
                    error = "数据库版本落后；请运行 `make migrate-db` 完成向前迁移"
                else:
                    schema_state = "uninitialized"
                    error = "数据库尚未初始化；请运行 `make setup-db`"
            return PostgresCheckResult(
                host=target.host,
                port=target.port,
                database=target.database,
                server_version=str(server_version),
                current_revision=current_revision,
                head_revision=head,
                revision_matches=current_revision == head,
                missing_tables=missing_tables,
                schema_state=schema_state,
                error=error,
            )
    except Exception:
        return PostgresCheckResult(
            host=target.host,
            port=target.port,
            database=target.database,
            head_revision=head,
            schema_state="unavailable",
            connected=False,
            error="无法连接或读取 PostgreSQL；请检查 DATABASE_URL、数据库状态和访问权限",
        )
    finally:
        try:
            await engine.dispose()
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
    print(f"Schema 状态: {result.schema_state}")
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
