"""只读检查 ActWeave PostgreSQL 连接与 schema 健康状态。"""

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
from deerflow.persistence.bootstrap import (
    CURRENT_SCHEMA_REVISION,
    M7RecreateRequired,
    classify_database,
)

try:
    from scripts.setup_postgres import parse_target
except ModuleNotFoundError:  # Direct ``python scripts/check_postgres.py`` execution.
    from setup_postgres import parse_target

REQUIRED_TABLES: tuple[str, ...] = (
    "agent_design_activities",
    "agent_design_operations",
    "agent_design_sessions",
    "agent_version_mcp_refs",
    "agent_version_skill_refs",
    "agent_versions",
    "agents",
    "artifacts",
    "asset_catalog_state",
    "audit_logs",
    "auth_sessions",
    "channel_external_principals",
    "channel_connections",
    "channel_conversations",
    "channel_credentials",
    "channel_inbound_deliveries",
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
    "execution_approval_output_delivery_candidates",
    "execution_approval_output_delivery_obligations",
    "execution_approval_requests",
    "execution_approval_result_receipts",
    "feedback",
    "file_chunks",
    "files",
    "job_attempts",
    "jobs",
    "memory_document_versions",
    "memory_documents",
    "memory_dream_runs",
    "memory_dream_prepare_runs",
    "memory_episodes",
    "memory_history_entries",
    "mcp_server_versions",
    "mcp_servers",
    "mcp_tool_discovery_attempts",
    "mcp_version_credential_slots",
    "project_mcp_tool_inventories",
    "project_invitation_rate_limits",
    "project_invitations",
    "project_channel_credential_bindings",
    "project_channel_group_binding_challenges",
    "project_channel_group_bindings",
    "project_channel_instance_leases",
    "project_channel_instances",
    "project_default_agents",
    "project_memberships",
    "project_quotas",
    "project_skill_credential_bindings",
    "project_skill_credential_configs",
    "project_system_agent_bindings",
    "project_system_mcp_bindings",
    "project_system_skill_bindings",
    "project_usage_counters",
    "project_usage_ledger",
    "projects",
    "run_asset_versions",
    "run_event_invariants",
    "run_event_partition_state",
    "run_events",
    "run_mcp_grant_snapshots",
    "run_memory_context_snapshots",
    "run_model_config_snapshots",
    "run_runtime_policy_snapshots",
    "run_skill_credential_snapshots",
    "runs",
    "scheduled_task_runs",
    "scheduled_tasks",
    "skill_design_activities",
    "skill_design_draft_files",
    "skill_design_operation_baseline_files",
    "skill_design_operations",
    "skill_design_sessions",
    "skill_version_files",
    "skill_versions",
    "skills",
    "store",
    "store_migrations",
    "system_model_catalog_state",
    "system_model_config_versions",
    "system_model_configs",
    "system_runtime_policy_catalog_state",
    "system_runtime_policy_versions",
    "system_runtime_policies",
    "system_asset_upgrade_audit",
    "thread_event_sequences",
    "threads_meta",
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
    pg_trgm_installed: bool = False
    schema_state: Literal[
        "ready",
        "uninitialized",
        "upgrade_required",
        "recreate_required",
        "unavailable",
    ] = "unavailable"
    connected: bool = True
    error: str = ""

    @property
    def healthy(self) -> bool:
        return self.connected and self.schema_state == "ready" and self.revision_matches and not self.missing_tables and self.pg_trgm_installed and not self.error


def get_schema_marker() -> str:
    return CURRENT_SCHEMA_REVISION


async def check_postgres(database_url: str) -> PostgresCheckResult:
    """只读检查连接、PostgreSQL 版本、schema marker 与必需表。"""
    target = parse_target(database_url)
    expected_marker = get_schema_marker()
    engine = create_async_engine(
        DatabaseConfig(url=database_url).sqlalchemy_url,
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            server_version = await connection.scalar(text("SELECT version()"))
            pg_trgm_installed = bool(await connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")))
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
                elif database_state == "behind":
                    schema_state = "upgrade_required"
                    error = f"数据库处于已知历史 revision（{current_revision}），链头为 {expected_marker}；请运行 `make upgrade-db`"
                else:
                    schema_state = "uninitialized"
                    error = "数据库尚未初始化；请运行 `make setup-db`"
            return PostgresCheckResult(
                host=target.host,
                port=target.port,
                database=target.database,
                server_version=str(server_version),
                current_revision=current_revision,
                head_revision=expected_marker,
                revision_matches=current_revision == expected_marker,
                missing_tables=missing_tables,
                pg_trgm_installed=pg_trgm_installed,
                schema_state=schema_state,
                error=error,
            )
    except Exception:
        return PostgresCheckResult(
            host=target.host,
            port=target.port,
            database=target.database,
            head_revision=expected_marker,
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
    print(f"当前 Schema marker: {result.current_revision or '缺失'}")
    print(f"目标 Schema marker: {result.head_revision or '未知'}")
    print(f"Schema 状态: {result.schema_state}")
    print(f"pg_trgm 扩展: {'已安装' if result.pg_trgm_installed else '缺失'}")
    if result.missing_tables:
        print(f"缺失表: {', '.join(result.missing_tables)}")
    if result.error:
        print(f"错误: {result.error}")


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="只读检查 ActWeave PostgreSQL 数据库").parse_args(argv)
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
