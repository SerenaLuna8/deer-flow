from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.bootstrap import _get_alembic_config, _get_head_revision
from scripts.check_postgres import check_postgres
from scripts.migrate_sqlite_to_postgres import backup_source, inspect_source, migrate_source
from scripts.setup_postgres import _asyncpg_url, migrate_postgres


def _fingerprint(path: Path) -> tuple[int, str]:
    return path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_admin_source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT,
            system_role TEXT NOT NULL, created_at TIMESTAMP NOT NULL,
            oauth_provider TEXT, oauth_id TEXT, needs_setup BOOLEAN NOT NULL,
            token_version INTEGER NOT NULL)"""
        )
        connection.execute(
            """INSERT INTO users VALUES
            ('00000000-0000-4000-8000-000000000001','admin@example.invalid',NULL,
            'admin','2026-07-12T00:00:00+00:00',NULL,NULL,0,0)"""
        )


@pytest.mark.integration
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_m1_cutover_preserves_source_and_bootstraps_default_project(
    tmp_path: Path,
    postgres_database_url: str,
) -> None:
    source = tmp_path / "legacy.db"
    _legacy_admin_source(source)
    sidecars = [source.with_name(f"{source.name}{suffix}") for suffix in ("-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.touch()
    before = {path.name: _fingerprint(path) for path in (source, *sidecars)}

    inspection = inspect_source(source)
    backup = backup_source(
        source,
        tmp_path / "backups",
        (inspection.inventory.sha256, inspection.inventory.size_bytes),
    )
    assert backup.sha256 == inspection.inventory.sha256
    assert backup.path.read_bytes() == source.read_bytes()

    engine = create_async_engine(postgres_database_url)
    try:
        config = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, config, "0004_migration_ledger")
    finally:
        await engine.dispose()

    dry_run = await migrate_source(backup.path, postgres_database_url, dry_run=True)
    assert dry_run.verified is True
    assert dry_run.tables["users"].planned_insert == 1
    migrated = await migrate_source(backup.path, postgres_database_url, dry_run=False)
    assert migrated.verified is True
    assert migrated.tables["users"].inserted == 1

    setup = await migrate_postgres(postgres_database_url)
    health = await check_postgres(postgres_database_url)
    assert setup.revision == _get_head_revision()
    assert health.healthy is True
    assert DatabaseConfig(url=postgres_database_url).sqlalchemy_url.startswith("postgresql+asyncpg://")

    connection = await asyncpg.connect(_asyncpg_url(postgres_database_url))
    try:
        project = await connection.fetchrow(
            """SELECT p.slug,p.created_by_user_id,m.user_id,m.role
            FROM projects p JOIN project_memberships m ON m.project_id=p.id
            WHERE p.slug='default-project'"""
        )
        assert project is not None
        assert dict(project) == {
            "slug": "default-project",
            "created_by_user_id": "00000000-0000-4000-8000-000000000001",
            "user_id": "00000000-0000-4000-8000-000000000001",
            "role": "admin",
        }
        assert await connection.fetchval("SELECT system_role FROM users") == "system_admin"
        assert await connection.fetchval("SELECT count(*) FROM migration_ledger") == 1
    finally:
        await connection.close()

    after = {path.name: _fingerprint(path) for path in (source, *sidecars)}
    assert after == before


def test_runtime_tree_has_no_sqlite_backend_dependency() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    paths = [
        *sorted((backend_root / "app").rglob("*.py")),
        *sorted((backend_root / "packages" / "harness" / "deerflow").rglob("*.py")),
        backend_root / "pyproject.toml",
        backend_root / "packages" / "harness" / "pyproject.toml",
        backend_root / "uv.lock",
    ]
    content = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for forbidden in (
        "sqlite+aiosqlite",
        "langgraph-checkpoint-sqlite",
        "langgraph.checkpoint.sqlite",
        "aiosqlite",
        "SQLiteUserRepository",
    ):
        assert forbidden not in content
