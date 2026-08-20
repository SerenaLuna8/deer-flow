"""显式升级 ActWeave PostgreSQL 数据库到迁移链头（唯一升级入口，见 D3）。

Gateway/Worker/Scheduler 永不自动迁移：behind 库在运行时只会 fail-closed 并指向
本脚本。升级流程为 分类 → 会话级 advisory lock → ``alembic upgrade head`` →
升级后重算 catalog digest 校验（必须与全新安装完全一致）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
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
_SYSTEM_ASSET_ID_NAMESPACE = uuid.UUID("6f6622dd-a1f5-5799-a2f7-d9f793ea8d2e")


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


@dataclass(frozen=True)
class AssetLifecycleInventory:
    project_agent_versions: int
    project_agent_current_versions: int
    project_agent_candidate_versions: int
    project_agent_historical_versions: int
    project_skill_versions: int
    project_skill_current_versions: int
    project_skill_candidate_versions: int
    project_skill_historical_versions: int
    agent_skill_references: int
    project_system_agent_bindings: int
    project_system_skill_bindings: int
    system_agents: int
    system_agent_versions: int
    system_skills: int
    system_skill_versions: int
    revoked_system_skill_versions: int
    runs: int
    recoverable_runs: int
    snapshotted_asset_rows: int


async def _asset_lifecycle_inventory(connection) -> AssetLifecycleInventory:
    """Collect only counts and reject unsafe legacy relationships read-only."""

    columns = {
        str(row[0])
        for row in await connection.execute(
            text(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name='agents'
                """,
            )
        )
    }
    pointer = "current_version_id" if "current_version_id" in columns else "current_published_version_id"
    values: dict[str, int] = {}
    for key, statement in {
        "project_agent_versions": "SELECT count(*) FROM agent_versions v JOIN agents a ON a.id=v.agent_id WHERE a.scope='project'",
        "project_skill_versions": "SELECT count(*) FROM skill_versions v JOIN skills s ON s.id=v.skill_id WHERE s.scope='project'",
        "system_agents": "SELECT count(*) FROM agents WHERE scope='system'",
        "system_agent_versions": "SELECT count(*) FROM agent_versions v JOIN agents a ON a.id=v.agent_id WHERE a.scope='system'",
        "system_skills": "SELECT count(*) FROM skills WHERE scope='system'",
        "system_skill_versions": "SELECT count(*) FROM skill_versions v JOIN skills s ON s.id=v.skill_id WHERE s.scope='system'",
        "revoked_system_skill_versions": "SELECT count(*) FROM skill_versions v JOIN skills s ON s.id=v.skill_id WHERE s.scope='system' AND v.revoked_at IS NOT NULL",
        "runs": "SELECT count(*) FROM runs",
        "snapshotted_asset_rows": "SELECT count(*) FROM run_asset_versions",
        "agent_skill_references": "SELECT count(*) FROM agent_version_skill_refs",
        "project_system_agent_bindings": "SELECT count(*) FROM project_system_agent_bindings",
        "project_system_skill_bindings": "SELECT count(*) FROM project_system_skill_bindings",
    }.items():
        values[key] = int(await connection.scalar(text(statement)) or 0)
    if pointer == "current_published_version_id":
        for kind, asset_table, version_table, parent_column in (
            ("agent", "agents", "agent_versions", "agent_id"),
            ("skill", "skills", "skill_versions", "skill_id"),
        ):
            current = int(
                await connection.scalar(
                    text(
                        f"SELECT count(*) FROM {asset_table} WHERE scope='project' AND {pointer} IS NOT NULL",
                    )
                )
                or 0
            )
            candidate = int(
                await connection.scalar(
                    text(
                        f"SELECT count(*) FROM {version_table} v JOIN {asset_table} a ON a.id=v.{parent_column} WHERE a.scope='project' AND v.workflow_status='draft' AND v.id IS DISTINCT FROM a.{pointer}",
                    )
                )
                or 0
            )
            total = values[f"project_{kind}_versions"]
            values[f"project_{kind}_current_versions"] = current
            values[f"project_{kind}_candidate_versions"] = candidate
            values[f"project_{kind}_historical_versions"] = total - current - candidate
    else:
        for kind, asset_table, version_table, parent_column in (
            ("agent", "agents", "agent_versions", "agent_id"),
            ("skill", "skills", "skill_versions", "skill_id"),
        ):
            current = int(
                await connection.scalar(
                    text(
                        f"SELECT count(*) FROM {asset_table} WHERE scope='project' AND {pointer} IS NOT NULL",
                    )
                )
                or 0
            )
            candidate = int(
                await connection.scalar(
                    text(
                        f"""
                        WITH RECURSIVE forward(asset_id,version_id) AS (
                          SELECT id,{pointer} FROM {asset_table}
                          WHERE scope='project' AND {pointer} IS NOT NULL
                          UNION ALL
                          SELECT child.{parent_column},child.id
                          FROM {version_table} child
                          JOIN forward parent
                            ON child.{parent_column}=parent.asset_id
                           AND child.supersedes_version_id=parent.version_id
                        )
                        SELECT count(*) FROM forward
                        JOIN {asset_table} asset ON asset.id=forward.asset_id
                        WHERE forward.version_id <> asset.{pointer}
                        """,
                    )
                )
                or 0
            )
            total = values[f"project_{kind}_versions"]
            values[f"project_{kind}_current_versions"] = current
            values[f"project_{kind}_candidate_versions"] = candidate
            values[f"project_{kind}_historical_versions"] = total - current - candidate
    if pointer == "current_published_version_id":
        try:
            from migrations.versions.current_asset_version_lifecycle import (
                _preflight_legacy_lifecycle,
            )

            await connection.run_sync(_preflight_legacy_lifecycle)
        except (RuntimeError, ValueError):
            raise PostgresUpgradeError(
                "ASSET_LIFECYCLE_PREFLIGHT_FAILED: Agent/Skill 血缘、校验和或 Run 闭包不完整",
            ) from None
        unrecoverable_asset_rows = int(
            await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM run_asset_versions snapshot
                    LEFT JOIN agent_versions agent
                      ON snapshot.asset_kind='agent' AND agent.id=snapshot.version_id
                    LEFT JOIN skill_versions skill
                      ON snapshot.asset_kind='skill' AND skill.id=snapshot.version_id
                    LEFT JOIN mcp_server_versions mcp
                      ON snapshot.asset_kind='mcp' AND mcp.id=snapshot.version_id
                    WHERE (snapshot.asset_kind='agent' AND agent.id IS NULL)
                       OR (snapshot.asset_kind='skill' AND skill.id IS NULL)
                       OR (snapshot.asset_kind='mcp' AND mcp.id IS NULL)
                    """,
                )
            )
            or 0
        )
    else:
        unrecoverable_asset_rows = int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM run_asset_versions WHERE snapshot_json IS NULL",
                )
            )
            or 0
        )
    if unrecoverable_asset_rows:
        raise PostgresUpgradeError(
            "ASSET_LIFECYCLE_PREFLIGHT_FAILED: Run 存在无法物化的资产版本引用",
        )
    values["recoverable_runs"] = int(
        await connection.scalar(
            text(
                """
                SELECT count(*) FROM (
                  SELECT project_id,owner_user_id,run_id
                  FROM run_asset_versions
                  GROUP BY project_id,owner_user_id,run_id
                ) recoverable
                """,
            )
        )
        or 0
    )
    if values["recoverable_runs"] != values["runs"]:
        raise PostgresUpgradeError(
            "ASSET_LIFECYCLE_PREFLIGHT_FAILED: Run 缺少完整资产快照",
        )

    missing_current = int(
        await connection.scalar(
            text(
                f"""
                SELECT count(*) FROM (
                  SELECT id FROM agents WHERE scope='system' AND {pointer} IS NULL
                  UNION ALL
                  SELECT id FROM skills WHERE scope='system' AND {pointer} IS NULL
                ) missing
                """,
            )
        )
        or 0
    )
    if missing_current:
        raise PostgresUpgradeError(
            "ASSET_LIFECYCLE_PREFLIGHT_FAILED: System Agent/Skill 缺少当前版本",
        )
    if pointer == "current_published_version_id":
        invalid_system_refs = int(
            await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM agents a
                    JOIN agent_versions av ON av.agent_id=a.id
                    JOIN agent_version_skill_refs ref ON ref.agent_version_id=av.id
                    JOIN skill_versions sv ON sv.id=ref.skill_version_id
                    JOIN skills s ON s.id=sv.skill_id
                    WHERE a.scope='system' AND s.scope<>'system'
                    """,
                )
            )
            or 0
        )
        if invalid_system_refs:
            raise PostgresUpgradeError(
                "ASSET_LIFECYCLE_PREFLIGHT_FAILED: System Agent 存在 Project Skill 引用",
            )
    else:
        system_rows = (
            await connection.execute(
                text(
                    """
                    SELECT source_key,current_version_id FROM agents
                    WHERE scope='system'
                    UNION ALL
                    SELECT source_key,current_version_id FROM skills
                    WHERE scope='system'
                    ORDER BY source_key
                    """,
                )
            )
        ).all()
        if any(
            not isinstance(source_key, str)
            or current_id
            != uuid.uuid5(
                _SYSTEM_ASSET_ID_NAMESPACE,
                f"{source_key}:version:1",
            )
            for source_key, current_id in system_rows
        ):
            raise PostgresUpgradeError(
                "ASSET_LIFECYCLE_PREFLIGHT_FAILED: System Current v1 标识不规范",
            )
    return AssetLifecycleInventory(**values)


def print_inventory(inventory: AssetLifecycleInventory) -> None:
    print("Agent/Skill 生命周期升级只读清单:")
    print(
        f"Project Agent 版本(总计/Current/Candidate/Historical): {inventory.project_agent_versions}/{inventory.project_agent_current_versions}/{inventory.project_agent_candidate_versions}/{inventory.project_agent_historical_versions}",
    )
    print(
        f"Project Skill 版本(总计/Current/Candidate/Historical): {inventory.project_skill_versions}/{inventory.project_skill_current_versions}/{inventory.project_skill_candidate_versions}/{inventory.project_skill_historical_versions}",
    )
    print(
        f"Agent→Skill 引用/Project→System Agent/Skill 绑定: {inventory.agent_skill_references}/{inventory.project_system_agent_bindings}/{inventory.project_system_skill_bindings}",
    )
    print(
        f"System 资产/版本: Agent={inventory.system_agents}/{inventory.system_agent_versions}, Skill={inventory.system_skills}/{inventory.system_skill_versions}, 已撤销 Skill 版本={inventory.revoked_system_skill_versions}",
    )
    print(
        f"Run/可恢复 Run/资产快照行: {inventory.runs}/{inventory.recoverable_runs}/{inventory.snapshotted_asset_rows}",
    )


def _run_alembic_upgrade_sync(database_url: str) -> None:
    """Run the chain synchronously; called via ``asyncio.to_thread``."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    config.attributes["sqlalchemy_url"] = database_url
    command.upgrade(config, "head")


async def upgrade_postgres(database_url: str) -> UpgradeResult:
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
) -> UpgradeResult:
    async with engine.connect() as connection:
        try:
            state = await classify_database(connection)
        except M7RecreateRequired:
            raise PostgresUpgradeError(f"M7_RECREATE_REQUIRED: 目标库的 marker 未知或 catalog 已漂移，不在受支持的迁移链上；请人工检查后显式重建（当前链头 {CURRENT_SCHEMA_REVISION}）") from None
        if state == "empty":
            raise PostgresUpgradeError("目标库为空；升级只服务存量库，请运行 `make setup-db` 全新安装")
        current_marker = str(await connection.scalar(text("SELECT version_num FROM alembic_version")))
        inventory = await _asset_lifecycle_inventory(connection)
        print_inventory(inventory)

    if state == "current":
        return UpgradeResult(
            host=target_host,
            port=target_port,
            database=target_database,
            from_revision=current_marker,
            to_revision=CURRENT_SCHEMA_REVISION,
            applied=False,
        )

    await asyncio.to_thread(_run_alembic_upgrade_sync, database_url)

    async with engine.connect() as connection:
        try:
            post_state = await classify_database(connection)
        except M7RecreateRequired:
            post_state = "drifted"
        if post_state != "current":
            raise PostgresUpgradeError("升级后校验失败：catalog 与全新安装不一致；请停止运行并按数据库恢复流程处理")
    return UpgradeResult(
        host=target_host,
        port=target_port,
        database=target_database,
        from_revision=current_marker,
        to_revision=CURRENT_SCHEMA_REVISION,
        applied=True,
    )


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
        "--preflight-only",
        action="store_true",
        help="只读输出 Agent/Skill 生命周期升级清单，不执行迁移",
    )
    return parser


async def preflight_postgres(database_url: str) -> AssetLifecycleInventory:
    target = parse_target(database_url)
    del target
    engine = create_async_engine(
        DatabaseConfig(url=database_url).sqlalchemy_url,
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            state = await classify_database(connection)
            if state == "empty":
                raise PostgresUpgradeError(
                    "目标库为空；升级清单只服务存量库",
                )
            return await _asset_lifecycle_inventory(connection)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("错误: 必须显式设置 DATABASE_URL", file=sys.stderr)
        return 2
    try:
        if args.preflight_only:
            inventory = asyncio.run(preflight_postgres(database_url))
            print_inventory(inventory)
            return 0
        result = asyncio.run(upgrade_postgres(database_url))
    except (PostgresUpgradeError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
