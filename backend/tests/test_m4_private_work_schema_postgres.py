from __future__ import annotations

import asyncio
import importlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import _get_alembic_config

M4_TABLES = {
    "run_asset_versions",
    "run_mcp_grant_snapshots",
    "files",
    "file_chunks",
    "artifacts",
    "user_project_memories",
    "user_project_memory_facts",
    "private_work_migration_runs",
    "private_work_migration_ledger",
    "private_work_cutover_state",
}


def test_m4_models_register_final_private_work_schema() -> None:
    importlib.import_module("deerflow.persistence.models")

    assert M4_TABLES <= set(Base.metadata.tables)
    thread_columns = set(Base.metadata.tables["threads_meta"].c.keys())
    assert {"project_id", "owner_user_id", "agent_asset_id", "agent_scope"} <= thread_columns
    assert "user_id" not in thread_columns

    for table_name in (
        "threads_meta",
        "runs",
        "run_events",
        "feedback",
        "run_asset_versions",
        "run_mcp_grant_snapshots",
        "files",
        "artifacts",
        "user_project_memories",
        "user_project_memory_facts",
        "channel_connections",
        "channel_oauth_states",
        "channel_conversations",
    ):
        table = Base.metadata.tables[table_name]
        assert table.c.project_id.nullable is False
        assert table.c.owner_user_id.nullable is False
        assert table.c.owner_user_id.type.length == 36


def test_m4_models_install_composite_scope_constraints_without_snapshot_secrets() -> None:
    importlib.import_module("deerflow.persistence.models")

    expected_constraints = {
        "threads_meta": {
            "uq_threads_meta_private_scope",
            "fk_threads_meta_project_membership",
        },
        "runs": {
            "uq_runs_private_scope",
            "fk_runs_private_thread",
            "fk_runs_project_membership",
        },
        "run_events": {"fk_run_events_private_run"},
        "feedback": {"fk_feedback_private_run"},
        "artifacts": {
            "fk_artifacts_private_run",
            "fk_artifacts_private_file",
        },
        "channel_conversations": {
            "fk_channel_conversations_private_connection",
            "fk_channel_conversations_private_thread",
        },
    }
    for table_name, names in expected_constraints.items():
        actual = {constraint.name for constraint in Base.metadata.tables[table_name].constraints}
        assert names <= actual

    file_indexes = {index.name: index for index in Base.metadata.tables["files"].indexes}
    active_path = file_indexes["uq_files_active_logical_path"]
    assert active_path.unique is True
    assert "deleted" in str(active_path.dialect_options["postgresql"]["where"])

    forbidden_fragments = ("secret", "envelope", "ciphertext", "nonce", "key_id", "locator")
    for table_name in ("run_asset_versions", "run_mcp_grant_snapshots"):
        columns = tuple(Base.metadata.tables[table_name].c.keys())
        assert not any(fragment in column for fragment in forbidden_fragments for column in columns)


def test_m4_finalize_revision_is_alembic_head() -> None:
    migration = importlib.import_module("deerflow.persistence.migrations.versions.0009_project_private_work_finalize")
    cfg = AlembicConfig()
    cfg.set_main_option(
        "script_location",
        str(Path(migration.__file__).resolve().parents[1]),
    )

    assert migration.revision == "0009_project_private_work_finalize"
    assert migration.down_revision == "0008_project_private_work_expand"
    assert ScriptDirectory.from_config(cfg).get_current_head() == migration.revision


def test_m4_finalize_validates_prerequisites_before_any_ddl(monkeypatch) -> None:
    migration = importlib.import_module("deerflow.persistence.migrations.versions.0009_project_private_work_finalize")
    mutations: list[str] = []
    fake_op = SimpleNamespace(
        get_bind=lambda: object(),
        alter_column=lambda *_args, **_kwargs: mutations.append("alter_column"),
        create_foreign_key=lambda *_args, **_kwargs: mutations.append("create_foreign_key"),
        create_unique_constraint=lambda *_args, **_kwargs: mutations.append("create_unique_constraint"),
        create_check_constraint=lambda *_args, **_kwargs: mutations.append("create_check_constraint"),
        create_index=lambda *_args, **_kwargs: mutations.append("create_index"),
        drop_constraint=lambda *_args, **_kwargs: mutations.append("drop_constraint"),
        drop_index=lambda *_args, **_kwargs: mutations.append("drop_index"),
    )
    monkeypatch.setattr(migration, "op", fake_op)

    def fail_before_ddl(_bind) -> None:
        raise RuntimeError("private-work finalize prerequisites are incomplete")

    monkeypatch.setattr(migration, "_assert_finalize_prerequisites", fail_before_ddl)
    with pytest.raises(RuntimeError, match="prerequisites are incomplete"):
        migration.upgrade()
    assert mutations == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_m4_finalize_schema_has_private_scope_and_composite_fks(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            scoped_tables = (
                "threads_meta",
                "runs",
                "run_events",
                "feedback",
                "run_asset_versions",
                "run_mcp_grant_snapshots",
                "files",
                "artifacts",
                "user_project_memories",
                "user_project_memory_facts",
                "channel_connections",
                "channel_oauth_states",
                "channel_conversations",
            )
            columns_by_table = {table: await connection.run_sync(lambda sync, table=table: {item["name"]: item for item in inspect(sync).get_columns(table)}) for table in scoped_tables}
            fks_by_table = {table: await connection.run_sync(lambda sync, table=table: inspect(sync).get_foreign_keys(table)) for table in scoped_tables}
            file_indexes = await connection.run_sync(lambda sync: inspect(sync).get_indexes("files"))
            revision = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            marker = (
                await connection.execute(
                    text(
                        """SELECT stage,migration_run_id,empty_domain_probe_complete,
                        checkpoint_marker_probe_complete,cutover_at
                        FROM private_work_cutover_state WHERE id=1"""
                    )
                )
            ).one()
        assert M4_TABLES <= tables
        for table in scoped_tables:
            assert columns_by_table[table]["project_id"]["nullable"] is False
            assert columns_by_table[table]["owner_user_id"]["nullable"] is False
            assert columns_by_table[table]["owner_user_id"]["type"].length == 36
            assert any(fk["constrained_columns"] == ["project_id", "owner_user_id"] and fk["referred_table"] == "project_memberships" and fk["referred_columns"] == ["project_id", "user_id"] for fk in fks_by_table[table])
        assert "user_id" not in columns_by_table["threads_meta"]
        assert any(fk["constrained_columns"] == ["project_id", "owner_user_id", "thread_id", "run_id"] and fk["referred_table"] == "runs" for fk in fks_by_table["artifacts"])
        assert any(fk["constrained_columns"] == ["project_id", "owner_user_id", "thread_id", "file_id"] and fk["referred_table"] == "files" for fk in fks_by_table["artifacts"])
        assert any(fk["constrained_columns"] == ["project_id", "owner_user_id", "connection_id"] and fk["referred_table"] == "channel_connections" for fk in fks_by_table["channel_conversations"])
        active_path = next(index for index in file_indexes if index["name"] == "uq_files_active_logical_path")
        assert active_path["unique"] is True
        assert "deleted" in str(active_path["dialect_options"]["postgresql_where"])
        forbidden_fragments = ("secret", "envelope", "ciphertext", "nonce", "key_id", "locator")
        for table in ("run_asset_versions", "run_mcp_grant_snapshots"):
            assert not any(fragment in column for fragment in forbidden_fragments for column in columns_by_table[table])
        assert marker.stage == "cutover_complete"
        assert marker.migration_run_id is None
        assert marker.empty_domain_probe_complete is True
        assert marker.checkpoint_marker_probe_complete is True
        assert marker.cutover_at is not None
        assert revision == "0009_project_private_work_finalize"
    finally:
        await engine.dispose()


def test_0009_downgrade_checks_for_private_work_data_before_any_ddl(monkeypatch) -> None:
    migration = importlib.import_module("deerflow.persistence.migrations.versions.0009_project_private_work_finalize")
    mutations: list[str] = []
    fake_result = SimpleNamespace(scalar_one=lambda: True)
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(execute=lambda _statement: fake_result),
        drop_constraint=lambda *_args, **_kwargs: mutations.append("drop_constraint"),
        drop_index=lambda *_args, **_kwargs: mutations.append("drop_index"),
        alter_column=lambda *_args, **_kwargs: mutations.append("alter_column"),
        create_primary_key=lambda *_args, **_kwargs: mutations.append("create_primary_key"),
        create_foreign_key=lambda *_args, **_kwargs: mutations.append("create_foreign_key"),
        create_unique_constraint=lambda *_args, **_kwargs: mutations.append("create_unique_constraint"),
        create_index=lambda *_args, **_kwargs: mutations.append("create_index"),
    )
    monkeypatch.setattr(migration, "op", fake_op)

    with pytest.raises(RuntimeError, match="private-work data exists"):
        migration.downgrade()
    assert mutations == []


def test_0008_downgrade_checks_for_backfilled_scope_before_any_ddl(monkeypatch) -> None:
    migration = importlib.import_module("deerflow.persistence.migrations.versions.0008_project_private_work_expand")
    mutations: list[str] = []

    def execute(statement):
        has_backfilled_scope = "project_id" in str(statement) and "IS NOT NULL" in str(statement)
        return SimpleNamespace(scalar_one=lambda: has_backfilled_scope)

    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(execute=execute),
        drop_table=lambda *_args, **_kwargs: mutations.append("drop_table"),
        drop_column=lambda *_args, **_kwargs: mutations.append("drop_column"),
        drop_index=lambda *_args, **_kwargs: mutations.append("drop_index"),
        alter_column=lambda *_args, **_kwargs: mutations.append("alter_column"),
    )
    monkeypatch.setattr(migration, "op", fake_op)

    with pytest.raises(RuntimeError, match="backfilled private scope exists"):
        migration.downgrade()
    assert mutations == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_0009_rejects_missing_migration_prerequisite_without_schema_change(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, cfg, "0008_project_private_work_expand")
        with pytest.raises(RuntimeError, match="private-work finalize prerequisites"):
            await asyncio.to_thread(command.upgrade, cfg, "head")
        async with engine.connect() as connection:
            revision = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            columns = await connection.run_sync(lambda sync: {item["name"] for item in inspect(sync).get_columns("threads_meta")})
        assert revision == "0008_project_private_work_expand"
        assert "user_id" in columns
        assert "owner_user_id" not in columns
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_0009_rejects_legacy_nulls_after_completed_probes_without_schema_change(
    postgres_database_url: str,
) -> None:
    migration = importlib.import_module("deerflow.persistence.migrations.versions.0009_project_private_work_finalize")
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, cfg, "0008_project_private_work_expand")
        migration_run_id = uuid.uuid4()
        digest = "a" * 64
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO threads_meta
                    (thread_id,user_id,status,metadata_json,created_at,updated_at)
                    VALUES ('legacy-thread','legacy-owner','idle','{}'::jsonb,now(),now())"""
                )
            )
            await connection.execute(
                text(
                    """INSERT INTO private_work_migration_runs
                    (id,mode,status,source_fingerprint,owner_map_digest,
                     legacy_source_probe_complete,checkpoint_marker_probe_complete,
                     cross_scope_probe_complete,completed_at)
                    VALUES (:id,'execute','completed',:digest,:digest,true,true,true,now())"""
                ),
                {"id": migration_run_id, "digest": digest},
            )
            await connection.execute(
                text(
                    """INSERT INTO private_work_migration_ledger
                    (migration_run_id,domain,source_key_hash,source_fingerprint,
                     target_digest,status,row_count,byte_count)
                    VALUES (:run_id,:domain,:digest,:digest,:digest,'complete',0,0)"""
                ),
                [{"run_id": migration_run_id, "domain": domain, "digest": f"{index:064x}"} for index, domain in enumerate(sorted(migration.FINALIZE_LEDGER_DOMAINS), start=1)],
            )

        with pytest.raises(RuntimeError, match="nullable private scope remains"):
            await asyncio.to_thread(command.upgrade, cfg, "head")
        async with engine.connect() as connection:
            revision = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            columns = await connection.run_sync(lambda sync: {item["name"] for item in inspect(sync).get_columns("threads_meta")})
        assert revision == "0008_project_private_work_expand"
        assert "user_id" in columns
        assert "owner_user_id" not in columns
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_0009_finalizes_completed_empty_domain_migration_and_downgrades_safely(
    postgres_database_url: str,
) -> None:
    migration = importlib.import_module("deerflow.persistence.migrations.versions.0009_project_private_work_finalize")
    engine = create_async_engine(postgres_database_url)
    try:
        cfg = _get_alembic_config(engine)
        await asyncio.to_thread(command.upgrade, cfg, "0008_project_private_work_expand")
        migration_run_id = uuid.uuid4()
        digest = "a" * 64
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO private_work_migration_runs
                    (id,mode,status,source_fingerprint,owner_map_digest,
                     legacy_source_probe_complete,checkpoint_marker_probe_complete,
                     cross_scope_probe_complete,completed_at)
                    VALUES (:id,'execute','completed',:digest,:digest,true,true,true,now())"""
                ),
                {"id": migration_run_id, "digest": digest},
            )
            await connection.execute(
                text(
                    """INSERT INTO private_work_migration_ledger
                    (migration_run_id,domain,source_key_hash,source_fingerprint,
                     target_digest,status,row_count,byte_count)
                    VALUES (:run_id,:domain,:digest,:digest,:digest,'complete',0,0)"""
                ),
                [{"run_id": migration_run_id, "domain": domain, "digest": f"{index:064x}"} for index, domain in enumerate(sorted(migration.FINALIZE_LEDGER_DOMAINS), start=1)],
            )

        await asyncio.to_thread(command.upgrade, cfg, "head")
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == migration.revision
            thread_columns = await connection.run_sync(lambda sync: {item["name"]: item for item in inspect(sync).get_columns("threads_meta")})
        assert thread_columns["project_id"]["nullable"] is False
        assert thread_columns["owner_user_id"]["nullable"] is False
        assert "user_id" not in thread_columns

        await asyncio.to_thread(command.downgrade, cfg, "0008_project_private_work_expand")
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0008_project_private_work_expand"
            columns = await connection.run_sync(lambda sync: {item["name"] for item in inspect(sync).get_columns("threads_meta")})
        assert "user_id" in columns
        assert "owner_user_id" not in columns

        await asyncio.to_thread(command.downgrade, cfg, "0007_project_shared_assets")
        # 0008 restores alembic_version VARCHAR(32); discard asyncpg prepared
        # statements compiled against its M4 VARCHAR(64) shape before probing.
        await engine.dispose()
        engine = create_async_engine(postgres_database_url)
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0007_project_shared_assets"
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        assert M4_TABLES.isdisjoint(tables)
    finally:
        await engine.dispose()
