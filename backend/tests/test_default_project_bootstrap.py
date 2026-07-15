from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic import command as alembic_command
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.bootstrap import bootstrap_default_project
from app.projects.errors import ProjectBootstrapFailed, ProjectDatabaseUnavailable
from app.projects.models import BootstrapStatus
from deerflow.persistence.bootstrap import (
    _filesystem_has_legacy_private_source,
    _get_alembic_config,
    _requires_explicit_private_work_migration,
    bootstrap_schema,
)


@pytest.mark.parametrize(
    ("revision", "expected"),
    [
        ("0007_project_shared_assets", True),
        ("0008_project_private_work_expand", True),
        ("0009_project_private_work_finalize", False),
        ("0010_private_file_source", False),
        ("0011_private_artifact_tombstone", False),
        ("0012_project_automation_expand", False),
        ("0013_project_automation_finalize", False),
        ("0010_unknown_future_revision", True),
    ],
)
def test_private_work_staged_boundary_requires_explicit_migration(
    revision: str,
    expected: bool,
) -> None:
    assert _requires_explicit_private_work_migration(revision) is expected


def test_private_work_filesystem_probe_detects_only_legacy_private_sources(tmp_path: Path) -> None:
    assert _filesystem_has_legacy_private_source(tmp_path) is False

    unrelated = tmp_path / "cache" / "state.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("{}")
    assert _filesystem_has_legacy_private_source(tmp_path) is False

    workspace = tmp_path / "users" / "owner" / "threads" / "thread" / "user-data" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "content.txt").write_text("legacy")
    assert _filesystem_has_legacy_private_source(tmp_path) is True


@pytest.mark.parametrize("source_kind", ["file", "symlink"])
def test_private_work_filesystem_probe_detects_root_thread_layout(
    tmp_path: Path,
    source_kind: str,
) -> None:
    workspace = tmp_path / "threads" / "legacy-thread" / "user-data" / "workspace"
    workspace.mkdir(parents=True)
    source = workspace / "legacy-source"
    if source_kind == "file":
        source.write_text("legacy")
    else:
        source.symlink_to(tmp_path / "missing-target")

    assert _filesystem_has_legacy_private_source(tmp_path) is True


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_bootstrap_routes_root_thread_files_to_explicit_migration(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "threads" / "legacy-thread" / "user-data" / "workspace"
    workspace.mkdir(parents=True)
    legacy_file = workspace / "legacy.txt"
    legacy_file.write_text("legacy")
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    engine = create_async_engine(postgres_database_url)
    try:
        with pytest.raises(RuntimeError, match="make migrate-private-work"):
            await bootstrap_schema(engine)

        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))
        assert tables == set()
        assert legacy_file.read_text() == "legacy"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_gateway_bootstrap_does_not_cross_m4_boundary_with_legacy_source(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(alembic_command.upgrade, cfg, "0007_project_shared_assets")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO threads_meta
                    (thread_id,user_id,status,metadata_json,created_at,updated_at)
                    VALUES ('legacy-thread','legacy-owner','idle','{}'::jsonb,now(),now())"""
                )
            )

        with pytest.raises(RuntimeError, match="make migrate-private-work"):
            await bootstrap_schema(engine)

        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            columns = await connection.run_sync(lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("threads_meta")})
        assert revision == "0007_project_shared_assets"
        assert "project_id" not in columns
        assert "owner_user_id" not in columns
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_gateway_bootstrap_empty_0007_database_requires_explicit_m4_migration(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(alembic_command.upgrade, cfg, "0007_project_shared_assets")

        with pytest.raises(RuntimeError, match="make migrate-private-work"):
            await bootstrap_schema(engine)

        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            columns = await connection.run_sync(lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("threads_meta")})
            private_rows = await connection.scalar(text("SELECT count(*) FROM threads_meta"))
            migration_runs = await connection.scalar(text("SELECT to_regclass('private_work_migration_runs')"))
            cutover_state = await connection.scalar(text("SELECT to_regclass('private_work_cutover_state')"))
        assert revision == "0007_project_shared_assets"
        assert "project_id" not in columns
        assert "owner_user_id" not in columns
        assert migration_runs is None
        assert cutover_state is None
        assert private_rows == 0
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_gateway_bootstrap_never_enters_empty_receipt_state_machine_for_versioned_database(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    probes = 0

    def filesystem_probe(_home: Path) -> bool:
        nonlocal probes
        probes += 1
        return False

    monkeypatch.setattr(
        "deerflow.persistence.bootstrap._filesystem_has_legacy_private_source",
        filesystem_probe,
    )
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(
            alembic_command.upgrade,
            cfg,
            "0007_project_shared_assets",
        )

        with pytest.raises(RuntimeError, match="make migrate-private-work"):
            await bootstrap_schema(engine)

        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            receipts = await connection.scalar(text("SELECT to_regclass('private_work_migration_runs')"))
            markers = await connection.scalar(text("SELECT to_regclass('private_work_cutover_state')"))
        assert probes == 0
        assert revision == "0007_project_shared_assets"
        assert receipts is None
        assert markers is None
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_gateway_bootstrap_requires_explicit_automation_migration_for_nonempty_0011(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(
            alembic_command.downgrade,
            cfg,
            "0011_private_artifact_tombstone",
        )
        await engine.dispose()
        engine = create_async_engine(postgres_database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO scheduled_tasks
                    (id,user_id,thread_id,context_mode,assistant_id,title,prompt,
                     schedule_type,schedule_spec,timezone,status,overlap_policy,
                     next_run_at,last_run_at,last_run_id,last_thread_id,last_error,
                     lease_owner,lease_expires_at,run_count,created_at,updated_at)
                    VALUES
                    ('legacy-task','legacy-owner',NULL,'fresh_thread_per_run',NULL,
                     'Legacy','private','once','{}'::json,'UTC','enabled','skip',
                     NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,now(),now())"""
                )
            )

        with pytest.raises(RuntimeError, match="automation migration required"):
            await bootstrap_schema(engine)

        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            columns = await connection.run_sync(lambda sync: {column["name"] for column in inspect(sync).get_columns("scheduled_tasks")})
        assert revision == "0011_private_artifact_tombstone"
        assert "project_id" not in columns
        assert "owner_user_id" not in columns
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_gateway_bootstrap_upgrades_empty_0011_automation_domain(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(
            alembic_command.downgrade,
            cfg,
            "0011_private_artifact_tombstone",
        )
        await engine.dispose()
        engine = create_async_engine(postgres_database_url)

        await bootstrap_schema(engine)

        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            marker = (
                await connection.execute(
                    text(
                        """SELECT stage,empty_domain_probe_complete,
                        final_schema_probe_complete,cutover_at
                        FROM automation_cutover_state WHERE id=1"""
                    )
                )
            ).one()
        assert revision == "0013_project_automation_finalize"
        assert marker.stage == "cutover_complete"
        assert marker.empty_domain_probe_complete is True
        assert marker.final_schema_probe_complete is True
        assert marker.cutover_at is not None
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_gateway_bootstrap_rejects_pre_alembic_private_rows_before_schema_changes(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """CREATE TABLE threads_meta (
                        thread_id VARCHAR PRIMARY KEY,
                        user_id VARCHAR NOT NULL,
                        status VARCHAR NOT NULL,
                        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL
                    )"""
                )
            )
            await connection.execute(
                text(
                    """INSERT INTO threads_meta
                    (thread_id,user_id,status,metadata_json,created_at,updated_at)
                    VALUES ('legacy-thread','legacy-owner','idle','{}'::jsonb,now(),now())"""
                )
            )

        with pytest.raises(RuntimeError, match="make migrate-private-work"):
            await bootstrap_schema(engine)

        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))
            columns = await connection.run_sync(lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("threads_meta")})
        assert tables == {"threads_meta"}
        assert "project_id" not in columns
        assert "owner_user_id" not in columns
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_bootstrap_rejects_unmarked_langgraph_rows_without_checkpoint_ddl(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    engine = create_async_engine(postgres_database_url)
    checkpoint_tables = {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}

    async def checkpoint_catalog() -> dict[str, tuple[tuple[str, str, bool], ...]]:
        async with engine.connect() as connection:
            return {
                table: await connection.run_sync(lambda sync_connection, table=table: tuple((column["name"], str(column["type"]), column["nullable"]) for column in inspect(sync_connection).get_columns(table)))
                for table in sorted(checkpoint_tables)
            }

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """CREATE TABLE checkpoints (
                        thread_id TEXT NOT NULL,
                        checkpoint_ns TEXT NOT NULL DEFAULT '',
                        checkpoint_id TEXT NOT NULL,
                        parent_checkpoint_id TEXT,
                        type TEXT,
                        checkpoint JSONB NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{}',
                        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                    )"""
                )
            )
            await connection.execute(
                text(
                    """CREATE TABLE checkpoint_blobs (
                        thread_id TEXT NOT NULL,
                        checkpoint_ns TEXT NOT NULL DEFAULT '',
                        channel TEXT NOT NULL,
                        version TEXT NOT NULL,
                        type TEXT NOT NULL,
                        blob BYTEA,
                        PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
                    )"""
                )
            )
            await connection.execute(
                text(
                    """CREATE TABLE checkpoint_writes (
                        thread_id TEXT NOT NULL,
                        checkpoint_ns TEXT NOT NULL DEFAULT '',
                        checkpoint_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        idx INTEGER NOT NULL,
                        channel TEXT NOT NULL,
                        type TEXT,
                        blob BYTEA NOT NULL,
                        task_path TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                    )"""
                )
            )
            await connection.execute(
                text(
                    """INSERT INTO checkpoints
                    (thread_id,checkpoint_ns,checkpoint_id,checkpoint,metadata)
                    VALUES ('legacy-thread','','checkpoint-1','{}'::jsonb,'{}'::jsonb)"""
                )
            )
        before = await checkpoint_catalog()

        with pytest.raises(RuntimeError, match="make migrate-private-work"):
            await bootstrap_schema(engine)

        after = await checkpoint_catalog()
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))
            row_count = await connection.scalar(text("SELECT count(*) FROM checkpoints"))
        assert after == before
        assert tables == checkpoint_tables
        assert row_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_bootstrap_states_idempotency_and_concurrency(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = str(uuid.uuid4())
    try:
        async with factory() as session:
            assert (await bootstrap_default_project(session)).status is BootstrapStatus.NO_USERS
        async with engine.begin() as connection:
            await connection.execute(
                text("""INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,'admin@example.com','user',:now,false,0)"""),
                {"id": admin_id, "now": datetime.now(UTC)},
            )
        async with factory() as session:
            assert (await bootstrap_default_project(session)).status is BootstrapStatus.WAITING_FOR_ADMIN
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE users SET system_role='system_admin' WHERE id=:id"), {"id": admin_id})

        async def run_once():
            async with factory() as session:
                return await bootstrap_default_project(session)

        results = await asyncio.gather(run_once(), run_once())
        assert {result.status for result in results} == {BootstrapStatus.CREATED, BootstrapStatus.EXISTING}
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT count(*) FROM projects WHERE slug='default-project'"))).scalar_one() == 1
            assert (
                await connection.execute(
                    text("""SELECT count(*) FROM project_memberships m
                JOIN projects p ON p.id=m.project_id WHERE p.slug='default-project'""")
                )
            ).scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("roles", [("user", "user"), ("system_admin", "system_admin")])
async def test_default_bootstrap_requires_a_unique_admin(migrated_postgres_database_url: str, roles: tuple[str, str]) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            for index, role in enumerate(roles):
                await connection.execute(
                    text("""INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,:role,:now,false,0)"""),
                    {
                        "id": str(uuid.uuid4()),
                        "email": f"u{index}@example.com",
                        "role": role,
                        "now": datetime.now(UTC),
                    },
                )
        async with factory() as session:
            with pytest.raises(ProjectBootstrapFailed) as exc_info:
                await bootstrap_default_project(session)
        assert exc_info.value.code == "AMBIGUOUS_BOOTSTRAP_ADMIN"
        assert "@example" not in str(exc_info.value)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_bootstrap_selects_unique_admin_among_existing_users(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            for index, role in enumerate(("user", "system_admin", "user")):
                user_id = admin_id if role == "system_admin" else str(uuid.uuid4())
                await connection.execute(
                    text(
                        """INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,:role,:now,false,0)"""
                    ),
                    {
                        "id": user_id,
                        "email": f"existing{index}@example.com",
                        "role": role,
                        "now": datetime.now(UTC),
                    },
                )
        async with factory() as session:
            result = await bootstrap_default_project(session)
        assert result.status is BootstrapStatus.CREATED
        async with engine.connect() as connection:
            owner, member, role = (
                await connection.execute(
                    text(
                        """SELECT p.created_by_user_id,m.user_id,m.role
                        FROM projects p JOIN project_memberships m ON m.project_id=p.id
                        WHERE p.slug='default-project'"""
                    )
                )
            ).one()
        assert (owner, member, role) == (admin_id, admin_id, "admin")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_bootstrap_rejects_slug_collision_without_mutation(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            for user_id, email, role in ((admin_id, "admin2@example.com", "system_admin"), (other_id, "other@example.com", "user")):
                await connection.execute(
                    text("""INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,:role,:now,false,0)"""),
                    {"id": user_id, "email": email, "role": role, "now": datetime.now(UTC)},
                )
            await connection.execute(
                text("""INSERT INTO projects
                (id,slug,display_name,created_by_user_id) VALUES (:id,'default-project','Wrong',:other)"""),
                {"id": uuid.uuid4(), "other": other_id},
            )
        async with factory() as session:
            with pytest.raises(ProjectBootstrapFailed) as exc_info:
                await bootstrap_default_project(session)
        assert exc_info.value.code == "DEFAULT_PROJECT_CONFLICT"
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT count(*) FROM project_memberships"))).scalar_one() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_bootstrap_rejects_partial_default_project(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = str(uuid.uuid4())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'partial@example.com','system_admin',:now,false,0)"""
                ),
                {"id": admin_id, "now": datetime.now(UTC)},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,created_by_user_id)
                    VALUES (:id,'default-project','Partial',:admin_id)"""
                ),
                {"id": uuid.uuid4(), "admin_id": admin_id},
            )
        async with factory() as session:
            with pytest.raises(ProjectBootstrapFailed) as exc_info:
                await bootstrap_default_project(session)
        assert exc_info.value.code == "DEFAULT_PROJECT_CONFLICT"
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT count(*) FROM project_memberships"))).scalar_one() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_bootstrap_database_error_is_sanitized() -> None:
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=DBAPIError(
            "SELECT secret FROM users",
            {"url": "postgresql://owner:password@db/private"},
            Exception("driver failed"),
            False,
        )
    )
    with pytest.raises(ProjectDatabaseUnavailable) as exc_info:
        await bootstrap_default_project(session)
    assert str(exc_info.value) == "Project storage unavailable"
    assert "SELECT" not in str(exc_info.value)
    assert "postgresql" not in str(exc_info.value)
